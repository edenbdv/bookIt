"""BOOK-65: Booking's handler for payment-events, against a real Postgres --
the Inbox row and the confirm/cancel have to commit together, which
FakeUnitOfWork can't show. Called directly with a payload, the way the
consumer will call it; the Kafka hop itself is tested separately.
"""

import logging
import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox.models import OutboxEvent, OutboxEventType
from app.reservation.inbox import ProcessedPaymentEvent
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.payment_events import handle_payment_event
from tests.integration.helpers import make_pending_reservation, reservation_status


def _payload(reservation: Reservation, event_type: str) -> dict:
    # the shape Payment's emit_payment_event writes -- the contract
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "reservation_id": str(reservation.id),
        "occurred_at": datetime.now(UTC).isoformat(),
        "payment_id": str(uuid.uuid4()),
        "amount_cents": reservation.total_cents,
    }


async def _handle(payload: dict) -> None:
    async with UnitOfWork() as uow:  # commits on clean exit, rolls back on exception
        await handle_payment_event(uow, payload)


async def _inbox_count(db_session: AsyncSession) -> int:
    return await db_session.scalar(select(func.count()).select_from(ProcessedPaymentEvent))


async def _booking_events(db_session: AsyncSession, reservation: Reservation) -> list:
    result = await db_session.scalars(
        select(OutboxEvent.event_type).where(OutboxEvent.reservation_id == reservation.id)
    )
    return list(result)


@pytest.mark.anyio
async def test_payment_succeeded_confirms_the_reservation(
    app_db: None, db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    await _handle(_payload(reservation, "payment_succeeded"))

    assert await reservation_status(db_session, reservation) == ReservationStatus.CONFIRMED
    # confirm still goes through Booking's own outbox -> Notification hears it
    assert await _booking_events(db_session, reservation) == [OutboxEventType.BOOKING_CONFIRMED]
    assert await _inbox_count(db_session) == 1


@pytest.mark.anyio
async def test_payment_failed_cancels_the_reservation(
    app_db: None, db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    await _handle(_payload(reservation, "payment_failed"))

    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED
    assert await _booking_events(db_session, reservation) == [OutboxEventType.BOOKING_CANCELLED]


# Kafka is at-least-once: the same message twice must act once
@pytest.mark.anyio
async def test_a_redelivered_event_acts_only_once(app_db: None, db_session: AsyncSession) -> None:
    reservation, _ = await make_pending_reservation(db_session)
    payload = _payload(reservation, "payment_succeeded")

    await _handle(payload)
    await _handle(payload)

    assert await reservation_status(db_session, reservation) == ReservationStatus.CONFIRMED
    assert await _booking_events(db_session, reservation) == [OutboxEventType.BOOKING_CONFIRMED]
    assert await _inbox_count(db_session) == 1


# while the saga's direct call still runs, the event usually arrives after
# the reservation is already confirmed -- a no-op, not an error
@pytest.mark.anyio
async def test_an_already_confirmed_reservation_is_left_alone(
    app_db: None, db_session: AsyncSession
) -> None:
    reservation, _ = await make_pending_reservation(db_session)
    await _handle(_payload(reservation, "payment_succeeded"))

    await _handle(_payload(reservation, "payment_succeeded"))  # a second, different event

    assert await _booking_events(db_session, reservation) == [OutboxEventType.BOOKING_CONFIRMED]
    assert await _inbox_count(db_session) == 2  # both marked handled


# money taken for a reservation that's already cancelled -- Booking can't use
# it, so it publishes booking_payment_rejected for Payment to refund
@pytest.mark.anyio
async def test_payment_succeeded_for_a_cancelled_reservation_is_rejected(
    app_db: None, db_session: AsyncSession, caplog: pytest.LogCaptureFixture
) -> None:
    reservation, _ = await make_pending_reservation(db_session)
    await db_session.execute(
        update(Reservation)
        .where(Reservation.id == reservation.id)
        .values(status=ReservationStatus.CANCELLED)
    )
    await db_session.commit()

    payload = _payload(reservation, "payment_succeeded")

    with caplog.at_level(logging.WARNING, logger="app.reservation.payment_events"):
        await _handle(payload)

    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED
    assert "rejecting it" in caplog.text
    [rejected] = (
        await db_session.scalars(
            select(OutboxEvent.payload).where(
                OutboxEvent.reservation_id == reservation.id,
                OutboxEvent.event_type == OutboxEventType.BOOKING_PAYMENT_REJECTED,
            )
        )
    ).all()
    # what Payment's refund needs -- which charge, and how much
    assert rejected["payment_id"] == payload["payment_id"]
    assert rejected["amount_cents"] == reservation.total_cents
    assert await _inbox_count(db_session) == 1  # same transaction as the event


# both or neither: if confirming fails, the Inbox row rolls back with it,
# so the redelivered event gets another chance instead of being skipped
@pytest.mark.anyio
async def test_a_failed_confirm_leaves_no_inbox_row(app_db: None, db_session: AsyncSession) -> None:
    reservation, _ = await make_pending_reservation(db_session)

    with (
        patch(
            "app.reservation.payment_events.service.confirm_reservation",
            side_effect=RuntimeError("simulated crash"),
        ),
        pytest.raises(RuntimeError),
    ):
        await _handle(_payload(reservation, "payment_succeeded"))

    assert await reservation_status(db_session, reservation) == ReservationStatus.PENDING
    assert await _inbox_count(db_session) == 0
