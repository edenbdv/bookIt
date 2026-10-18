"""BOOK-61: the payment saga's commit boundaries against a real Postgres --
what FakeUnitOfWork can't show, since its commit() persists nothing.

Ported from scripts/verify_saga_commit_split.py (BOOK-54), plus BOOK-55's
commit-before-provider-call guarantee. Both simulate a crash at a precise
point and then check what actually survived in the DB. Service-level, not
HTTP: the crash is injected with patch(), and what's under test is commit
order, not the router.
"""

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.core.db.unit_of_work import UnitOfWork
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.saga import create_payment_and_react
from app.payment.schemas import PaymentCreate
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import make_property, make_user

CHECK_IN = datetime.now(UTC).date() + timedelta(days=30)


@pytest.fixture
async def pending_reservation(db_session: AsyncSession) -> tuple[Reservation, User]:
    host = await make_user(db_session, "host@test.com")
    guest = await make_user(db_session, "guest@test.com")
    prop = await make_property(db_session, host)
    reservation = Reservation(
        property_id=prop.id,
        guest_id=guest.id,
        check_in=CHECK_IN,
        check_out=CHECK_IN + timedelta(days=4),
        guest_count=2,
        total_cents=40000,
        status=ReservationStatus.PENDING,
    )
    db_session.add(reservation)
    await db_session.commit()
    return reservation, guest


# column selects, not ORM objects -- always read fresh from the DB, never
# from db_session's identity map
async def _payments(session: AsyncSession, reservation: Reservation) -> list[tuple]:
    result = await session.execute(
        select(Payment.kind, Payment.status).where(Payment.reservation_id == reservation.id)
    )
    return list(result.all())


async def _reservation_status(session: AsyncSession, reservation: Reservation) -> str:
    return await session.scalar(select(Reservation.status).where(Reservation.id == reservation.id))


# BOOK-54: charge succeeds, then confirm_reservation() crashes. The charge's
# record must survive (it's committed on its own, before the reaction step),
# and the saga must compensate -- refund + release -- not just survive.
@pytest.mark.anyio
async def test_crash_after_charge_keeps_the_charge_and_compensates(
    app_db: None, db_session: AsyncSession, pending_reservation: tuple[Reservation, User]
) -> None:
    reservation, guest = pending_reservation

    with (
        patch(
            "app.payment.saga.reservation_service.confirm_reservation",
            side_effect=RuntimeError("simulated crash after charge succeeded"),
        ),
        pytest.raises(RuntimeError),  # compensating doesn't mean hiding the failure
    ):
        async with UnitOfWork() as uow:
            await create_payment_and_react(
                uow,
                PaymentCreate(reservation_id=reservation.id),
                guest.id,
                "commit-split",
                force=PaymentOutcome.SUCCESS,
            )

    payments = await _payments(db_session, reservation)
    assert (PaymentKind.CHARGE, PaymentStatus.SUCCEEDED) in payments  # charge not lost
    assert any(kind == PaymentKind.REFUND for kind, _ in payments)  # and compensated
    assert await _reservation_status(db_session, reservation) == ReservationStatus.CANCELLED


# BOOK-55: the PENDING payment row is committed *before* calling the
# provider -- so a crash during the call still leaves a row (and its id, the
# correlation id) for the webhook/reconciliation to resolve later
@pytest.mark.anyio
async def test_crash_during_provider_call_leaves_a_pending_payment(
    app_db: None, db_session: AsyncSession, pending_reservation: tuple[Reservation, User]
) -> None:
    reservation, guest = pending_reservation

    with (
        patch(
            "app.payment.service.charge",
            side_effect=RuntimeError("simulated crash mid-call to the provider"),
        ),
        pytest.raises(RuntimeError),
    ):
        async with UnitOfWork() as uow:
            await create_payment_and_react(
                uow, PaymentCreate(reservation_id=reservation.id), guest.id, "mid-call-crash"
            )

    assert await _payments(db_session, reservation) == [(PaymentKind.CHARGE, PaymentStatus.PENDING)]
    # nothing reacted to an outcome nobody knows yet
    assert await _reservation_status(db_session, reservation) == ReservationStatus.PENDING
