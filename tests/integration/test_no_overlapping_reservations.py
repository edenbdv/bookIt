"""BOOK-61: the no_overlapping_reservations EXCLUDE constraint's actual
behavior against a real Postgres -- ported from
scripts/verify_no_overlapping_reservations.py (BOOK-24).

test_migrations.py only proves the constraint *exists*; these prove what it
*does*. Raw inserts on purpose, not the reservation service: this is the
DB-layer guarantee on its own, the last line of defense under BOOK-34's
service logic (docs/db/exclude-constraint-basics.md).
"""

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus

SEP_1, SEP_3, SEP_5, SEP_7, SEP_8 = (date(2026, 9, d) for d in (1, 3, 5, 7, 8))


async def _make_property(session: AsyncSession, host_email: str = "host@test.com") -> Property:
    host = User(email=host_email, hashed_password="x")
    session.add(host)
    await session.flush()
    prop = Property(
        host_id=host.id,
        title="Cabin",
        description="Nice cabin",
        city="Tel Aviv",
        country="IL",
        max_guests=4,
        price_per_night_cents=10000,
    )
    session.add(prop)
    await session.flush()
    return prop


async def _make_guest(session: AsyncSession) -> User:
    guest = User(email="guest@test.com", hashed_password="x")
    session.add(guest)
    await session.flush()
    return guest


def _reservation(
    prop: Property,
    guest: User,
    check_in: date,
    check_out: date,
    status: ReservationStatus = ReservationStatus.CONFIRMED,
) -> Reservation:
    return Reservation(
        property_id=prop.id,
        guest_id=guest.id,
        check_in=check_in,
        check_out=check_out,
        guest_count=2,
        total_cents=40000,
        status=status,
    )


async def _count_reservations(session: AsyncSession) -> int:
    return await session.scalar(select(func.count()).select_from(Reservation))


@pytest.mark.anyio
async def test_overlapping_reservation_is_rejected(db_session: AsyncSession) -> None:
    prop = await _make_property(db_session)
    guest = await _make_guest(db_session)
    db_session.add(_reservation(prop, guest, SEP_1, SEP_5))
    await db_session.commit()

    db_session.add(_reservation(prop, guest, SEP_3, SEP_7))
    with pytest.raises(IntegrityError) as exc_info:
        await db_session.flush()

    # the right constraint fired -- not some unrelated FK/check failure
    assert "no_overlapping_reservations" in str(exc_info.value.orig)
    await db_session.rollback()
    assert await _count_reservations(db_session) == 1


@pytest.mark.anyio
async def test_same_dates_on_another_property_are_allowed(db_session: AsyncSession) -> None:
    prop_a = await _make_property(db_session, host_email="host-a@test.com")
    prop_b = await _make_property(db_session, host_email="host-b@test.com")
    guest = await _make_guest(db_session)

    db_session.add_all(
        [_reservation(prop_a, guest, SEP_1, SEP_5), _reservation(prop_b, guest, SEP_1, SEP_5)]
    )
    await db_session.commit()

    assert await _count_reservations(db_session) == 2


# '[)' range: check-out day is free, so same-day turnover isn't an overlap
@pytest.mark.anyio
async def test_back_to_back_reservations_are_allowed(db_session: AsyncSession) -> None:
    prop = await _make_property(db_session)
    guest = await _make_guest(db_session)

    db_session.add_all(
        [_reservation(prop, guest, SEP_1, SEP_5), _reservation(prop, guest, SEP_5, SEP_8)]
    )
    await db_session.commit()

    assert await _count_reservations(db_session) == 2


# WHERE (status <> 'cancelled'): cancelled rows don't take part in the check
@pytest.mark.anyio
async def test_overlap_with_a_cancelled_reservation_is_allowed(db_session: AsyncSession) -> None:
    prop = await _make_property(db_session)
    guest = await _make_guest(db_session)

    db_session.add_all(
        [
            _reservation(prop, guest, SEP_1, SEP_5),
            _reservation(prop, guest, SEP_3, SEP_7, status=ReservationStatus.CANCELLED),
        ]
    )
    await db_session.commit()

    assert await _count_reservations(db_session) == 2


# the real-world version of the case above: cancelling an existing booking
# is what frees its dates for someone else
@pytest.mark.anyio
async def test_cancelling_frees_the_dates(db_session: AsyncSession) -> None:
    prop = await _make_property(db_session)
    guest = await _make_guest(db_session)
    first = _reservation(prop, guest, SEP_1, SEP_5)
    db_session.add(first)
    await db_session.commit()

    first.status = ReservationStatus.CANCELLED
    await db_session.commit()

    db_session.add(_reservation(prop, guest, SEP_3, SEP_7))
    await db_session.commit()

    assert await _count_reservations(db_session) == 2
