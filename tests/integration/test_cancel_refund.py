"""BOOK-65 safety net: cancel -> refund end to end, through the real HTTP
endpoint and a real Postgres. Today the refund is a direct call in
payment/cancellation.py; BOOK-65 moves it to an event consumer. These tests
only check what a guest or host can observe (the response, the refund row),
so they should keep passing unchanged across that move.
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.property.models import CancellationPolicy, Property
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import auth_headers, make_pending_reservation, reservation_status


async def _make_paid_reservation(
    db_session: AsyncSession, policy: CancellationPolicy
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
    db_session.add(
        Payment(
            reservation_id=reservation.id,
            kind=PaymentKind.CHARGE,
            amount_cents=reservation.total_cents,
            status=PaymentStatus.SUCCEEDED,
        )
    )
    await db_session.commit()
    host = await db_session.scalar(
        select(User)
        .join(Property, Property.host_id == User.id)
        .where(Property.id == reservation.property_id)
    )
    return reservation, guest, host


# the refund row itself, not just the response -- after BOOK-65 these come
# from two different services, and both must agree on the amount
async def _refund_amounts(db_session: AsyncSession, reservation: Reservation) -> list[int]:
    result = await db_session.scalars(
        select(Payment.amount_cents).where(
            Payment.reservation_id == reservation.id, Payment.kind == PaymentKind.REFUND
        )
    )
    return list(result)


# STRICT + 30 days ahead -> 50%, not the full amount -- proves the policy
# was applied, not just that some refund happened
@pytest.mark.anyio
async def test_guest_cancels_a_paid_reservation_and_gets_a_policy_refund(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    reservation, guest, _ = await _make_paid_reservation(db_session, CancellationPolicy.STRICT)

    response = await api_client.post(
        f"/reservations/{reservation.id}/cancel", headers=auth_headers(guest)
    )

    assert response.status_code == 200
    assert response.json()["refund_amount_cents"] == 20000
    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED
    assert await _refund_amounts(db_session, reservation) == [20000]


# BOOK-80: same STRICT property, but the host cancels -> always the full
# 40000, the policy never applies to a host cancel
@pytest.mark.anyio
async def test_host_cancels_a_paid_reservation_and_the_guest_gets_a_full_refund(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    reservation, _, host = await _make_paid_reservation(db_session, CancellationPolicy.STRICT)

    response = await api_client.post(
        f"/reservations/{reservation.id}/host-cancel", headers=auth_headers(host)
    )

    assert response.status_code == 200
    assert response.json()["refund_amount_cents"] == 40000
    assert await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED
    assert await _refund_amounts(db_session, reservation) == [40000]
