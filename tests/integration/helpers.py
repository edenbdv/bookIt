"""Shared test-data builders for the integration suite -- users, properties,
auth headers. Plain functions, not fixtures: each test says exactly what it
creates, in the order it needs it."""

import uuid
from datetime import UTC, date, datetime, timedelta

from payment.models import Payment
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.auth.security import create_access_token, new_id
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus


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


async def make_pending_reservation(session: AsyncSession) -> tuple[Reservation, User]:
    """A host, a guest and a PENDING reservation awaiting payment -- the
    starting point of every payment-saga test. Committed, so app code
    reading through its own UnitOfWork (app_db) can see it."""
    host = await make_user(session, "host@test.com")
    guest = await make_user(session, "guest@test.com")
    prop = await make_property(session, host)
    check_in = datetime.now(UTC).date() + timedelta(days=30)
    reservation = Reservation(
        property_id=prop.id,
        guest_id=guest.id,
        check_in=check_in,
        check_out=check_in + timedelta(days=4),
        guest_count=2,
        total_cents=40000,
        status=ReservationStatus.PENDING,
    )
    session.add(reservation)
    await session.commit()
    return reservation, guest


# column selects, not ORM objects -- always read fresh from the DB, never
# from the session's identity map, which wouldn't see app code's writes
async def payment_rows(session: AsyncSession, reservation: Reservation) -> list[tuple]:
    result = await session.execute(
        select(Payment.id, Payment.kind, Payment.status).where(
            Payment.reservation_id == reservation.id
        )
    )
    return list(result.all())


async def reservation_status(session: AsyncSession, reservation: Reservation) -> str:
    return await session.scalar(select(Reservation.status).where(Reservation.id == reservation.id))


def booking(prop: Property, check_in: date, nights: int, guest_count: int = 2) -> dict[str, object]:
    return {
        "property_id": str(prop.id),
        "check_in": check_in.isoformat(),
        "check_out": (check_in + timedelta(days=nights)).isoformat(),
        "guest_count": guest_count,
    }
