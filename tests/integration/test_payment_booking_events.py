"""BOOK-65: Payment's handler for booking-events, against a real Postgres --
the Inbox row must commit with the PENDING refund row, before the provider
call, which FakeUnitOfWork can't show. Called directly with a payload, the
way the consumer will call it.
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.payment.booking_events import handle_booking_event
from app.payment.db import PaymentUnitOfWork
from app.payment.models import Payment, PaymentKind, PaymentStatus, ProcessedBookingEvent
from app.reservation.models import Reservation
from tests.integration.helpers import make_pending_reservation


async def _charge(
    db_session: AsyncSession,
    payment_db_session: AsyncSession,
    status: PaymentStatus = PaymentStatus.SUCCEEDED,
) -> tuple[Reservation, Payment]:
    # the reservation in Booking's database, the charge in Payment's (BOOK-65)
    reservation, _ = await make_pending_reservation(db_session)  # total_cents 40000
    charge = Payment(
        reservation_id=reservation.id,
        kind=PaymentKind.CHARGE,
        amount_cents=reservation.total_cents,
        status=status,
    )
    payment_db_session.add(charge)
    await payment_db_session.commit()
    return reservation, charge


def _rejected(charge: Payment, amount_cents: int | None = None) -> dict:
    # the shape Booking's reject_payment writes -- the contract
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "booking_payment_rejected",
        "reservation_id": str(charge.reservation_id),
        "occurred_at": datetime.now(UTC).isoformat(),
        "payment_id": str(charge.id),
        "amount_cents": charge.amount_cents if amount_cents is None else amount_cents,
    }


async def _handle(payload: dict) -> None:
    async with PaymentUnitOfWork() as uow:
        await handle_booking_event(uow, payload)


async def _refunds(payment_db_session: AsyncSession, reservation: Reservation) -> list[tuple]:
    result = await payment_db_session.execute(
        select(Payment.amount_cents, Payment.status).where(
            Payment.reservation_id == reservation.id, Payment.kind == PaymentKind.REFUND
        )
    )
    return list(result.all())


async def _inbox_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(select(func.count()).select_from(ProcessedBookingEvent))


# the event says 1 cent on purpose: the refund must follow Payment's own
# charge row (40000), not the amount another service sent
@pytest.mark.anyio
async def test_a_rejected_payment_is_refunded_in_full_from_the_charge_row(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)

    await _handle(_rejected(charge, amount_cents=1))

    [(amount, _)] = await _refunds(payment_db_session, reservation)
    assert amount == 40000
    assert await _inbox_count(payment_db_session) == 1


# Kafka is at-least-once: the same message twice must refund once
@pytest.mark.anyio
async def test_a_redelivered_rejection_refunds_only_once(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)
    payload = _rejected(charge)

    await _handle(payload)
    await _handle(payload)

    assert len(await _refunds(payment_db_session, reservation)) == 1


# nothing was charged -> nothing to give back
@pytest.mark.anyio
async def test_a_rejection_for_a_failed_charge_refunds_nothing(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session, PaymentStatus.FAILED)

    await _handle(_rejected(charge))

    assert await _refunds(payment_db_session, reservation) == []
    assert await _inbox_count(payment_db_session) == 1  # still marked handled


# Payment reads all of booking-events -- the other types are skipped, with no
# Inbox row (it only guards events that actually do something)
@pytest.mark.anyio
async def test_other_booking_events_are_skipped(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)
    payload = {**_rejected(charge), "event_type": "booking_confirmed"}

    await _handle(payload)

    assert await _refunds(payment_db_session, reservation) == []
    assert await _inbox_count(payment_db_session) == 0


# the reason the Inbox row is added first: a crash during the provider call
# must leave Inbox + PENDING refund both saved, so a redelivery doesn't start
# a second refund. Reconcile finishes the PENDING one.
@pytest.mark.anyio
async def test_a_crash_during_the_provider_call_cannot_cause_a_second_refund(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)
    payload = _rejected(charge)

    with (
        patch("app.payment.service.provider_refund", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError),
    ):
        await _handle(payload)

    assert await _refunds(payment_db_session, reservation) == [(40000, PaymentStatus.PENDING)]
    assert await _inbox_count(payment_db_session) == 1

    await _handle(payload)  # Kafka redelivers -- the offset was never committed

    assert len(await _refunds(payment_db_session, reservation)) == 1


def _cancelled(charge: Payment, refund_cents: int) -> dict:
    # the shape Booking's cancel functions write -- refund_cents is their policy decision
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": "booking_cancelled",
        "reservation_id": str(charge.reservation_id),
        "occurred_at": datetime.now(UTC).isoformat(),
        "cancelled_by": "guest",
        "refund_cents": refund_cents,
    }


@pytest.mark.anyio
async def test_a_cancellation_refunds_the_amount_booking_decided(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)

    await _handle(_cancelled(charge, refund_cents=20000))

    assert [amount for amount, _ in await _refunds(payment_db_session, reservation)] == [20000]
    assert await _inbox_count(payment_db_session) == 1


# never give back more than was charged, whatever the event says
@pytest.mark.anyio
async def test_a_cancellation_refund_is_capped_at_the_charge(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)

    await _handle(_cancelled(charge, refund_cents=999_999))

    assert [amount for amount, _ in await _refunds(payment_db_session, reservation)] == [40000]


@pytest.mark.anyio
async def test_a_redelivered_cancellation_refunds_only_once(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)
    payload = _cancelled(charge, refund_cents=20000)

    await _handle(payload)
    await _handle(payload)

    assert len(await _refunds(payment_db_session, reservation)) == 1


# refund_cents 0 -- unpaid, or a policy that gives nothing back: no effect, no Inbox row
@pytest.mark.anyio
async def test_a_cancellation_with_nothing_to_refund_is_skipped(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session)

    await _handle(_cancelled(charge, refund_cents=0))

    assert await _refunds(payment_db_session, reservation) == []
    assert await _inbox_count(payment_db_session) == 0


# Booking says refund, but Payment has no succeeded charge -- nothing to give back
@pytest.mark.anyio
async def test_a_cancellation_without_a_succeeded_charge_refunds_nothing(
    app_db: None, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, charge = await _charge(db_session, payment_db_session, PaymentStatus.FAILED)

    await _handle(_cancelled(charge, refund_cents=20000))

    assert await _refunds(payment_db_session, reservation) == []
