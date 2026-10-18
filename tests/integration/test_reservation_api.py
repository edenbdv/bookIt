"""BOOK-61: double booking through the full path -- HTTP -> router ->
service -> DB -> back to an HTTP response.

test_no_overlapping_reservations.py proves the EXCLUDE constraint on its
own, with raw inserts. This proves the layers above it handle that
constraint correctly: the second overlapping request gets a clean 409
(BOOK-35's ReservationConflictError mapping), not a 500 from an unhandled
IntegrityError. Sequential on purpose -- the *concurrent* version is BOOK-62.
"""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import create_access_token, new_id
from app.property.models import Property
from app.reservation.models import Reservation

# relative to today, not fixed dates -- ReservationCreate rejects a
# check_in in the past (schemas.py, BOOK-96)
CHECK_IN = datetime.now(UTC).date() + timedelta(days=30)


async def _make_user(session: AsyncSession, email: str) -> User:
    user = User(email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    return user


def _auth_headers(user: User) -> dict[str, str]:
    # a real access token, minted the way login does -- get_current_user
    # decodes it and loads the user from the test DB
    token = create_access_token(user.id, new_id())
    return {"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())}


def _booking(prop: Property, check_in: date, nights: int) -> dict[str, object]:
    return {
        "property_id": str(prop.id),
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=nights)).isoformat(),
        "guest_count": 2,
    }


@pytest.mark.anyio
async def test_overlapping_booking_via_api_returns_409(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    host = await _make_user(db_session, "host@test.com")
    # two different guests, each with its own Idempotency-Key -- so this is
    # about slot contention, not idempotent replay of one request
    guest_a = await _make_user(db_session, "guest-a@test.com")
    guest_b = await _make_user(db_session, "guest-b@test.com")
    prop = Property(
        host_id=host.id,
        title="Cabin",
        description="Nice cabin",
        city="Tel Aviv",
        country="IL",
        max_guests=4,
        price_per_night_cents=10000,
    )
    db_session.add(prop)
    await db_session.commit()

    first = await api_client.post(
        "/reservations", json=_booking(prop, CHECK_IN, nights=4), headers=_auth_headers(guest_a)
    )
    assert first.status_code == 201

    # starts 2 days into the first stay -- overlaps it
    second = await api_client.post(
        "/reservations",
        json=_booking(prop, CHECK_IN + timedelta(days=2), nights=4),
        headers=_auth_headers(guest_b),
    )
    assert second.status_code == 409
    assert second.json()["detail"] == "These dates are no longer available for this property"

    count = await db_session.scalar(select(func.count()).select_from(Reservation))
    assert count == 1
