"""BOOK-61: the payment's commit boundaries against a real Postgres --
what FakeUnitOfWork can't show, since its commit() persists nothing.

BOOK-55's commit-before-provider-call guarantee: simulate a crash at a
precise point, then check what actually survived in the DB. (BOOK-54's
"crash after charge -> compensate" test went with BOOK-65: confirming moved
to Booking's payment-events consumer, and a payment it can't use is
refunded via booking_payment_rejected -- tests/integration/test_payment_booking_*.py.)
"""

from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.payment.db import PaymentUnitOfWork
from app.payment.models import PaymentKind, PaymentStatus
from app.payment.schemas import PaymentCreate
from app.payment.service import pay_reservation
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import make_pending_reservation, payment_rows, reservation_status

# BOOK-66: paying asks Booking over HTTP -- reach it in-process
pytestmark = pytest.mark.usefixtures("booking_in_process")


@pytest.fixture
async def pending_reservation(db_session: AsyncSession) -> tuple[Reservation, User]:
    return await make_pending_reservation(db_session)


# BOOK-55: the PENDING payment row is committed *before* calling the
# provider -- so a crash during the call still leaves a row (and its id, the
# correlation id) for the webhook/reconciliation to resolve later
@pytest.mark.anyio
async def test_crash_during_provider_call_leaves_a_pending_payment(
    app_db: None,
    db_session: AsyncSession,
    payment_db_session: AsyncSession,
    pending_reservation: tuple[Reservation, User],
) -> None:
    reservation, guest = pending_reservation

    with (
        patch(
            "app.payment.service.charge",
            side_effect=RuntimeError("simulated crash mid-call to the provider"),
        ),
        pytest.raises(RuntimeError),
    ):
        async with PaymentUnitOfWork() as uow:
            await pay_reservation(
                uow, PaymentCreate(reservation_id=reservation.id), guest.id, "mid-call-crash"
            )

    payments = [
        (kind, status) for _, kind, status in await payment_rows(payment_db_session, reservation)
    ]
    assert payments == [(PaymentKind.CHARGE, PaymentStatus.PENDING)]
    # nothing reacted to an outcome nobody knows yet
    assert await reservation_status(db_session, reservation) == ReservationStatus.PENDING
