"""Cancel -> refund end to end, through the real HTTP endpoint and a real
Postgres. Since BOOK-65 the refund isn't part of the request: Booking writes
booking_cancelled (with refund_cents) and Payment refunds when it reads it.
_deliver_to_payment stands in for the relay + Kafka hop, which is tested
separately (test_payment_booking_consumer.py).
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.core.outbox.models import OutboxEvent, OutboxEventType
from app.payment.booking_events import handle_booking_event
from app.payment.db import PaymentUnitOfWork
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.property.models import CancellationPolicy, Property
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import auth_headers, make_pending_reservation, reservation_status


async def _make_paid_reservation(
    db_session: AsyncSession, payment_db_session: AsyncSession, policy: CancellationPolicy
) -> tuple[Reservation, User, User]:
    """A CONFIRMED reservation with a succeeded charge for its full total
    (40000, check_in in 30 days), on a property with the given policy.
    Returns (reservation, guest, host)."""
    reservation, guest = await make_pending_reservation(db_session)
    await db_session.execute(
        update(Property)
        .where(Property.id == reservation.property_id)
        .values(cancellation_policy=policy)
    )
    await db_session.execute(
        update(Reservation)
        .where(Reservation.id == reservation.id)
        .values(status=ReservationStatus.CONFIRMED)
    )
    # the charge lives in Payment's database (BOOK-65)
    payment_db_session.add(
        Payment(
            reservation_id=reservation.id,
            kind=PaymentKind.CHARGE,
            amount_cents=reservation.total_cents,
            status=PaymentStatus.SUCCEEDED,
        )
    )
    await db_session.commit()
    await payment_db_session.commit()
    host = await db_session.scalar(
        select(User)
        .join(Property, Property.host_id == User.id)
        .where(Property.id == reservation.property_id)
    )
    return reservation, guest, host


# the refund row itself, not just the response -- after BOOK-65 these come
# from two different services, and both must agree on the amount
async def _refund_amounts(payment_db_session: AsyncSession, reservation: Reservation) -> list[int]:
    result = await payment_db_session.scalars(
        select(Payment.amount_cents).where(
            Payment.reservation_id == reservation.id, Payment.kind == PaymentKind.REFUND
        )
    )
    return list(result)


# BOOK-65: what Payment will refund from -- the booking_cancelled payload
async def _cancelled_event(db_session: AsyncSession, reservation: Reservation) -> dict:
    return await db_session.scalar(
        select(OutboxEvent.payload).where(
            OutboxEvent.reservation_id == reservation.id,
            OutboxEvent.event_type == OutboxEventType.BOOKING_CANCELLED,
        )
    )


# what Payment's consumer does once booking_cancelled reaches it
async def _deliver_to_payment(db_session: AsyncSession, reservation: Reservation) -> None:
    payload = await _cancelled_event(db_session, reservation)
    async with PaymentUnitOfWork() as uow:
        await handle_booking_event(uow, payload)


# STRICT + 30 days ahead -> 50%, not the full amount -- proves the policy
# was applied, not just that some refund happened
@pytest.mark.anyio
async def test_guest_cancels_a_paid_reservation_and_gets_a_policy_refund(
    api_client: AsyncClient, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, guest, _ = await _make_paid_reservation(
        db_session, payment_db_session, CancellationPolicy.STRICT
    )

    response = await api_client.post(
        f"/reservations/{reservation.id}/cancel", headers=auth_headers(guest)
    )

    assert response.status_code == 200
    assert response.json()["refund_amount_cents"] == 20000
    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED
    assert (
        await _refund_amounts(payment_db_session, reservation) == []
    )  # not in the request anymore
    await _deliver_to_payment(db_session, reservation)
    assert await _refund_amounts(payment_db_session, reservation) == [20000]
    event = await _cancelled_event(db_session, reservation)
    assert (event["cancelled_by"], event["refund_cents"]) == ("guest", 20000)


# BOOK-80: same STRICT property, but the host cancels -> always the full
# 40000, the policy never applies to a host cancel
@pytest.mark.anyio
async def test_host_cancels_a_paid_reservation_and_the_guest_gets_a_full_refund(
    api_client: AsyncClient, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, _, host = await _make_paid_reservation(
        db_session, payment_db_session, CancellationPolicy.STRICT
    )

    response = await api_client.post(
        f"/reservations/{reservation.id}/host-cancel", headers=auth_headers(host)
    )

    assert response.status_code == 200
    assert response.json()["refund_amount_cents"] == 40000
    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED
    assert (
        await _refund_amounts(payment_db_session, reservation) == []
    )  # not in the request anymore
    await _deliver_to_payment(db_session, reservation)
    assert await _refund_amounts(payment_db_session, reservation) == [40000]
    event = await _cancelled_event(db_session, reservation)
    assert (event["cancelled_by"], event["refund_cents"]) == ("host", 40000)


# nothing was paid yet -> nothing to refund, whoever cancels
@pytest.mark.anyio
async def test_guest_cancels_an_unpaid_reservation_with_nothing_to_refund(
    api_client: AsyncClient, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, guest = await make_pending_reservation(db_session)

    response = await api_client.post(
        f"/reservations/{reservation.id}/cancel", headers=auth_headers(guest)
    )

    assert response.json()["refund_amount_cents"] == 0
    await _deliver_to_payment(db_session, reservation)
    assert await _refund_amounts(payment_db_session, reservation) == []
    event = await _cancelled_event(db_session, reservation)
    assert (event["cancelled_by"], event["refund_cents"]) == ("guest", 0)
