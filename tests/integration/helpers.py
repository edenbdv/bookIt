"""Shared test-data builders for the integration suite -- users, properties,
auth headers. Plain functions, not fixtures: each test says exactly what it
creates, in the order it needs it."""

import uuid
from datetime import date, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import create_access_token, new_id
from app.property.models import Property


async def make_user(session: AsyncSession, email: str) -> User:
    user = User(email=email, hashed_password="x")
    session.add(user)
    await session.flush()
    return user


async def make_property(session: AsyncSession, host: User, max_guests: int = 4) -> Property:
    prop = Property(
        host_id=host.id,
        title="Cabin",
        description="Nice cabin",
        city="Tel Aviv",
        country="IL",
        max_guests=max_guests,
        price_per_night_cents=10000,
    )
    session.add(prop)
    await session.flush()
    return prop


def auth_headers(user: User, idempotency_key: str | None = None) -> dict[str, str]:
    # a real access token, minted the way login does -- get_current_user
    # decodes it and loads the user from the test DB. A fresh random
    # Idempotency-Key unless the test needs to control it.
    token = create_access_token(user.id, new_id())
    return {
        "Authorization": f"Bearer {token}",
        "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
    }


def booking(prop: Property, check_in: date, nights: int, guest_count: int = 2) -> dict[str, object]:
    return {
        "property_id": str(prop.id),
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=nights)).isoformat(),
        "guest_count": guest_count,
    }
