"""BOOK-62: the booking-concurrency contract test -- N simultaneous booking
requests for the same property and dates produce exactly one reservation.

The one guarantee that must survive any architecture change (Epic 11), so
it's written against the public API only -- POST /reservations through
api_client -- never against service internals. Two layers are under test
together: reservation/service.py's per-property advisory lock (serializes
inserts, so GiST can't deadlock) and the no_overlapping_reservations EXCLUDE
constraint (the actual no-double-booking rule). Supersedes
scripts/verify_concurrent_reservations.py. Concepts: testing-basics.md §4.
"""

import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.models import User
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from tests.integration.helpers import auth_headers, booking, make_property, make_user

# enough to actually land inside the check-then-insert window, still fast.
# More than the app engine's default pool (5 + 10 overflow) on purpose: the
# extra requests queue for a connection instead of being dropped.
CONCURRENT_REQUESTS = 20
CHECK_IN = datetime.now(UTC).date() + timedelta(days=30)


# No email stub needed (BOOK-64): a new reservation used to call Celery's
# blocking .delay() (~4s per booking with no RabbitMQ, testing-decisions.md
# §11); it now writes a BookingCreated outbox row in the same transaction.


@pytest.fixture
async def contested_listing(db_session: AsyncSession) -> tuple[Property, list[User]]:
    host = await make_user(db_session, "host@test.com")
    prop = await make_property(db_session, host)
    # one guest per request -- so this is about slot contention, not
    # idempotency replay or one user's rate limit
    guests = [
        await make_user(db_session, f"guest-{i}@test.com") for i in range(CONCURRENT_REQUESTS)
    ]
    await db_session.commit()
    return prop, guests


async def _burst(client: AsyncClient, prop: Property, guests: list[User]) -> list[Response]:
    """Every request waits on one shared start signal, then all fire at once
    -- without it, gather() starts them one by one and the earliest may finish
    before the last has even begun (testing-basics.md §4.4)."""
    start = asyncio.Event()

    async def attempt(guest: User) -> Response:
        # each request goes through the app's real get_uow, so each gets its
        # own session/connection -- genuinely concurrent at the DB level
        # (testing-basics.md §4.2), not queued behind one shared session
        headers = auth_headers(guest)  # a fresh Idempotency-Key per attempt
        await start.wait()
        return await client.post(
            "/reservations", json=booking(prop, CHECK_IN, nights=4), headers=headers
        )

    tasks = [asyncio.create_task(attempt(guest)) for guest in guests]
    await asyncio.sleep(0)  # let every task reach start.wait()
    start.set()
    return await asyncio.gather(*tasks)


async def _active_reservations(session: AsyncSession, prop: Property) -> int:
    return await session.scalar(
        select(func.count())
        .select_from(Reservation)
        .where(
            Reservation.property_id == prop.id,
            Reservation.status != ReservationStatus.CANCELLED,
        )
    )


def _assert_exactly_one_winner(responses: list[Response]) -> Response:
    # the invariant, never *which* request won -- that's nondeterministic
    statuses = Counter(r.status_code for r in responses)
    assert statuses == {201: 1, 409: len(responses) - 1}, statuses  # and so: zero 500s
    [winner] = [r for r in responses if r.status_code == 201]
    return winner


@pytest.mark.anyio
async def test_concurrent_bookings_for_the_same_dates_create_exactly_one(
    api_client: AsyncClient,
    db_session: AsyncSession,
    contested_listing: tuple[Property, list[User]],
) -> None:
    prop, guests = contested_listing

    responses = await _burst(api_client, prop, guests)

    _assert_exactly_one_winner(responses)
    assert await _active_reservations(db_session, prop) == 1


# the constraint's WHERE status <> 'cancelled' under contention: once the
# winner cancels through the API, the dates are genuinely free again -- a
# second burst also gets exactly one winner, not zero (dates stuck) and not
# several (the cancelled row no longer guarding anything)
@pytest.mark.anyio
async def test_cancelling_the_winner_frees_the_dates_for_a_new_burst(
    api_client: AsyncClient,
    db_session: AsyncSession,
    contested_listing: tuple[Property, list[User]],
) -> None:
    prop, guests = contested_listing
    responses = await _burst(api_client, prop, guests)
    winner = _assert_exactly_one_winner(responses)
    # _burst returns responses in guest order, so the winner's index is its guest's
    winner_guest = guests[responses.index(winner)]

    cancel = await api_client.post(
        f"/reservations/{winner.json()['id']}/cancel", headers=auth_headers(winner_guest)
    )
    assert cancel.status_code == 200

    # everyone except the guest who just cancelled races again
    second_responses = await _burst(api_client, prop, [g for g in guests if g is not winner_guest])

    _assert_exactly_one_winner(second_responses)
    assert await _active_reservations(db_session, prop) == 1
