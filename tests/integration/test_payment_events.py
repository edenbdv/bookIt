"""BOOK-65: the payment-events outbox row against a real Postgres -- it must
be committed together with the payment's resolved status, never one without
the other. FakeUnitOfWork can't show that (its commit() persists nothing);
tests/payment/test_payment_event.py covers the payload itself.
"""

import uuid
from unittest.mock import patch

import pytest
from payment.db import PaymentUnitOfWork
from payment.models import Payment, PaymentEventType, PaymentOutboxEvent, PaymentStatus
from payment.provider import PaymentOutcome
from payment.schemas import PaymentCreate
from payment.service import pay_reservation, resolve_payment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.reservation.models import Reservation
from tests.integration.helpers import make_pending_reservation

# BOOK-66: paying asks Booking over HTTP -- reach it in-process
pytestmark = pytest.mark.usefixtures("booking_in_process")


@pytest.fixture
async def pending_reservation(db_session: AsyncSession) -> tuple[Reservation, User]:
    return await make_pending_reservation(db_session)


# column selects -- read fresh from the DB, not the session's identity map
async def _payment_status(
    payment_db_session: AsyncSession, reservation: Reservation
) -> list[tuple]:
    result = await payment_db_session.execute(
        select(Payment.id, Payment.status).where(Payment.reservation_id == reservation.id)
    )
    return list(result.all())


async def _events(payment_db_session: AsyncSession, reservation: Reservation) -> list[tuple]:
    result = await payment_db_session.execute(
        select(PaymentOutboxEvent.event_type, PaymentOutboxEvent.payload).where(
            PaymentOutboxEvent.reservation_id == reservation.id
        )
    )
    return list(result.all())


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("outcome", "status", "event_type"),
    [
        (PaymentOutcome.SUCCESS, PaymentStatus.SUCCEEDED, PaymentEventType.PAYMENT_SUCCEEDED),
        (PaymentOutcome.FAILURE, PaymentStatus.FAILED, PaymentEventType.PAYMENT_FAILED),
    ],
)
async def test_a_resolved_charge_commits_its_event_with_its_status(
    app_db: None,
    db_session: AsyncSession,
    payment_db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
    outcome: PaymentOutcome,
    status: PaymentStatus,
    event_type: PaymentEventType,
) -> None:
    reservation, guest = pending_reservation

    async with PaymentUnitOfWork() as uow:
        await pay_reservation(
            uow,
            PaymentCreate(reservation_id=reservation.id),
            guest.id,
            str(uuid.uuid4()),
            force=outcome,
        )

    [(payment_id, payment_status)] = await _payment_status(payment_db_session, reservation)
    assert payment_status == status
    [(saved_type, payload)] = await _events(payment_db_session, reservation)
    assert saved_type == event_type
    assert payload["payment_id"] == str(payment_id)


# the other half of "together": no resolved status -> no event. A crash
# mid-call to the provider leaves the charge PENDING (BOOK-55's commit
# split), and nothing has been decided yet to tell Booking about.
@pytest.mark.anyio
async def test_a_charge_left_pending_has_no_event(
    app_db: None,
    db_session: AsyncSession,
    payment_db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation

    with (
        patch("payment.service.charge", side_effect=RuntimeError("simulated crash")),
        pytest.raises(RuntimeError),
    ):
        async with PaymentUnitOfWork() as uow:
            await pay_reservation(
                uow, PaymentCreate(reservation_id=reservation.id), guest.id, str(uuid.uuid4())
            )

    [(_, payment_status)] = await _payment_status(payment_db_session, reservation)
    assert payment_status == PaymentStatus.PENDING
    assert await _events(payment_db_session, reservation) == []


# the webhook/reconcile path: PENDING -> resolved later, and only then does
# the event appear -- committed by resolve_payment's own commit
@pytest.mark.anyio
async def test_resolving_a_pending_charge_later_commits_its_event(
    app_db: None,
    db_session: AsyncSession,
    payment_db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation
    async with PaymentUnitOfWork() as uow:
        await pay_reservation(
            uow,
            PaymentCreate(reservation_id=reservation.id),
            guest.id,
            str(uuid.uuid4()),
            force=PaymentOutcome.TIMEOUT,
        )
    [(payment_id, _)] = await _payment_status(payment_db_session, reservation)
    assert await _events(payment_db_session, reservation) == []  # nothing yet

    async with PaymentUnitOfWork() as uow:
        await resolve_payment(uow, payment_id, PaymentOutcome.SUCCESS)

    [(_, payment_status)] = await _payment_status(payment_db_session, reservation)
    assert payment_status == PaymentStatus.SUCCEEDED
    [(saved_type, payload)] = await _events(payment_db_session, reservation)
    assert saved_type == PaymentEventType.PAYMENT_SUCCEEDED
    assert payload["payment_id"] == str(payment_id)
