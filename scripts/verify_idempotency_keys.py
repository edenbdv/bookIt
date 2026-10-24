"""Manual verification for the Idempotency-Key handling in reservations (BOOK-36).

Not a pytest test -- same reasoning as verify_no_overlapping_reservations.py
(BOOK-24/35): there's no test suite in this project yet (BOOK-61/62 for the
planned real thing). This runs real requests through UnitOfWork/service.py
against the dockerized dev Postgres and prints what actually happened, so
this ticket's behavior can be re-checked by hand later without re-deriving
it from scratch. Mirrors the live curl testing already done for BOOK-36.

Requires: `docker compose up -d db` already running.
Run with: uv run python scripts/verify_idempotency_keys.py
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from payment.models import Payment  # noqa: F401
from sqlalchemy import text

from app.auth.models import User
from app.core.db.engine import get_engine
from app.core.db.unit_of_work import UnitOfWork
from app.property.models import Property
from app.reservation.schemas import ReservationCreate
from app.reservation.service import (
    GuestCountExceededError,
    IdempotencyKeyInProgressError,
    IdempotencyKeyMismatchError,
    ReservationConflictError,
    create_reservation_idempotent,
)
from app.review.models import Review  # noqa: F401

DEMO_EMAIL_SUFFIX = "_idempotency_verify@example.com"

# relative to whenever this actually runs, not a hardcoded calendar date --
# otherwise this script quietly goes stale (check_in can't be in the past, BOOK-96)
TODAY = datetime.now(UTC).date()


async def main() -> None:
    async with UnitOfWork() as uow:
        host = User(email=f"host{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        guest_a = User(email=f"guestA{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        guest_b = User(email=f"guestB{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        uow.session.add_all([host, guest_a, guest_b])
        await uow.session.flush()

        prop = Property(
            host_id=host.id,
            title="Idempotency Test Villa",
            description="test",
            city="Tel Aviv",
            country="IL",
            max_guests=4,
            price_per_night_cents=10000,
        )
        uow.session.add(prop)
        await uow.session.flush()
        print(f"Created property {prop.id}")
        guest_a_id, guest_b_id, prop_id = guest_a.id, guest_b.id, prop.id

    # 1. First request with key K1 -- creates a reservation
    data1 = ReservationCreate(
        property_id=prop_id,
        check_in=TODAY + timedelta(days=30),
        check_out=TODAY + timedelta(days=34),
        guest_count=2,
    )
    async with UnitOfWork() as uow:
        r1 = await create_reservation_idempotent(uow, data1, guest_a_id, "K1-verify")
        r1_id = r1.id
    print(f"1. First request, key K1 -- created reservation {r1_id}")

    # 2. Retry with the SAME key K1 -- must return the SAME reservation, not a new one
    async with UnitOfWork() as uow:
        r1_retry = await create_reservation_idempotent(uow, data1, guest_a_id, "K1-verify")
    ok = r1_retry.id == r1_id
    print(
        f"2. Retry, same key K1 -- got reservation {r1_retry.id} -- {'OK' if ok else 'UH OH, different row!'}"
    )

    # 2b. Same key K1, but a DIFFERENT request body (different guest_count) --
    #     must be rejected explicitly, not silently return r1 and not silently
    #     create a second reservation either
    mismatched_data = ReservationCreate(
        property_id=prop_id,
        check_in=TODAY + timedelta(days=30),
        check_out=TODAY + timedelta(days=34),
        guest_count=1,
    )
    try:
        async with UnitOfWork() as uow:
            await create_reservation_idempotent(uow, mismatched_data, guest_a_id, "K1-verify")
        print("2b. Same key K1, different guest_count -- UH OH, should have been rejected")
    except IdempotencyKeyMismatchError:
        print("2b. Same key K1, different guest_count -- REJECTED as expected (no silent reuse)")

    # 3. A different guest reusing guest A's completed key -- must be rejected, not leak r1
    # NOTE: the try/except wraps the whole `async with` block, not just the call --
    # a raised domain error must propagate out of the block so UnitOfWork.__aexit__
    # sees it and rolls back. Catching it *inside* the block instead leaves an
    # aborted Postgres transaction that a later `commit()` would crash on.
    try:
        async with UnitOfWork() as uow:
            await create_reservation_idempotent(uow, data1, guest_b_id, "K1-verify")
        print("3. Guest B reuses guest A's key K1 -- UH OH, should have been rejected")
    except IdempotencyKeyInProgressError:
        print("3. Guest B reuses guest A's key K1 -- REJECTED as expected (no leak)")

    # 4. Same guest, NEW key, SAME overlapping dates -- must still hit the
    #    EXCLUDE constraint (BOOK-35). Changing the key must not bypass it.
    try:
        async with UnitOfWork() as uow:
            await create_reservation_idempotent(uow, data1, guest_a_id, "K2-verify")
        print("4. New key K2, same overlapping dates -- UH OH, should have conflicted")
    except ReservationConflictError:
        print(
            "4. New key K2, same overlapping dates -- REJECTED as expected (real double-booking still blocked)"
        )

    # 5. A failed attempt (bad guest_count) must NOT permanently burn the key --
    #    flush() isn't commit(), so the whole request (including the PROCESSING
    #    row) rolls back on the way out. A retry with the same key + valid data
    #    must then succeed.
    bad_data = ReservationCreate(
        property_id=prop_id,
        check_in=TODAY + timedelta(days=60),
        check_out=TODAY + timedelta(days=64),
        guest_count=99,
    )
    try:
        async with UnitOfWork() as uow:
            await create_reservation_idempotent(uow, bad_data, guest_a_id, "K3-verify")
        print("5a. Key K3, invalid guest_count -- UH OH, should have failed")
    except GuestCountExceededError:
        print("5a. Key K3, invalid guest_count -- REJECTED as expected")

    good_data = ReservationCreate(
        property_id=prop_id,
        check_in=TODAY + timedelta(days=60),
        check_out=TODAY + timedelta(days=64),
        guest_count=2,
    )
    async with UnitOfWork() as uow:
        r3 = await create_reservation_idempotent(uow, good_data, guest_a_id, "K3-verify")
    print(
        f"5b. Retry SAME key K3, valid data -- created reservation {r3.id} -- key was NOT burned, as expected"
    )

    # 6. Concurrent-duplicate-key race: two genuinely simultaneous requests
    #    with the same brand-new key -- must not create two reservations.
    race_data = ReservationCreate(
        property_id=prop_id,
        check_in=TODAY + timedelta(days=90),
        check_out=TODAY + timedelta(days=94),
        guest_count=2,
    )

    async def one_request() -> object:
        async with UnitOfWork() as uow:
            return await create_reservation_idempotent(uow, race_data, guest_a_id, "K4-verify")

    results = await asyncio.gather(one_request(), one_request(), return_exceptions=True)
    ids = {r.id for r in results if not isinstance(r, Exception)}
    print(f"6. Concurrent race, same new key K4 -- distinct reservation ids returned: {ids}")
    print(
        f"   {'OK, single reservation' if len(ids) == 1 else 'UH OH, duplicate reservations created'}"
    )

    # scoped to what THIS script created -- an unscoped `DELETE FROM reservations`/
    # `DELETE FROM properties` (the pattern verify_no_overlapping_reservations.py
    # uses) would wipe every row in those tables, not just this script's test
    # data, and can fail outright if any of them are FK-referenced elsewhere
    # (payments, in this case)
    async with get_engine().begin() as conn:
        await conn.execute(text("DELETE FROM idempotency_keys WHERE key LIKE '%-verify'"))
        await conn.execute(
            text("DELETE FROM reservations WHERE property_id = :pid"), {"pid": prop_id}
        )
        await conn.execute(text("DELETE FROM properties WHERE id = :pid"), {"pid": prop_id})
        await conn.execute(text(f"DELETE FROM users WHERE email LIKE '%{DEMO_EMAIL_SUFFIX}'"))
    print("cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
