"""BOOK-61: double booking through the full path -- HTTP -> router ->
service -> DB -> back to an HTTP response.

test_no_overlapping_reservations.py proves the EXCLUDE constraint on its
own, with raw inserts. This proves the layers above it handle that
constraint correctly: the second overlapping request gets a clean 409
(BOOK-35's ReservationConflictError mapping), not a 500 from an unhandled
IntegrityError. Sequential on purpose -- the *concurrent* version is BOOK-62.
"""

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.reservation.models import Reservation
from tests.integration.helpers import auth_headers, booking, make_property, make_user

# relative to today, not fixed dates -- ReservationCreate rejects a
# check_in in the past (schemas.py, BOOK-96)
CHECK_IN = datetime.now(UTC).date() + timedelta(days=30)


@pytest.mark.anyio
async def test_overlapping_booking_via_api_returns_409(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    host = await make_user(db_session, "host@test.com")
    # two different guests, each with its own Idempotency-Key -- so this is
    # about slot contention, not idempotent replay of one request
    guest_a = await make_user(db_session, "guest-a@test.com")
    guest_b = await make_user(db_session, "guest-b@test.com")
    prop = await make_property(db_session, host)
    await db_session.commit()

    first = await api_client.post(
        "/reservations", json=booking(prop, CHECK_IN, nights=4), headers=auth_headers(guest_a)
    )
    assert first.status_code == 201

    # starts 2 days into the first stay -- overlaps it
    second = await api_client.post(
        "/reservations",
        json=booking(prop, CHECK_IN + timedelta(days=2), nights=4),
        headers=auth_headers(guest_b),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "These dates are no longer available for this property"

    count = await db_session.scalar(select(func.count()).select_from(Reservation))
    assert count == 1
