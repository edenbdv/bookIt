"""Manual verification that BOOK-34/35's double-booking protection holds under
real concurrency, through the actual HTTP API (BOOK-39).

Not a pytest test -- same reasoning as verify_no_overlapping_reservations.py
(BOOK-24): there's no real test suite yet (see BOOK-61/BOOK-62 for testcontainers-
based integration tests and a proper booking-concurrency contract test). That
script proves the EXCLUDE constraint holds at the DB layer directly, with
sequential inserts on one session -- it never actually races anything. This
script fires genuinely concurrent POST /reservations requests at the running
API instead, so BOOK-35's error handling and BOOK-34's service logic sit in
the loop too, not just the raw constraint.

Assumes the script's local Settings (`app.core.config.get_settings()`) and the
running app's resolve to the same `secret_key` -- true by default (both fall
back to "dev-secret-change-me" with no .env override), which is what lets a
token minted here be accepted by the server as a different process.

Requires: `docker compose up -d` (db + app) already running.
Run with: uv run python scripts/verify_concurrent_reservations.py
"""

import asyncio
import sys
import uuid
from collections import Counter
from pathlib import Path
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

from app.auth.models import User
from app.auth.security import create_access_token, new_id
from app.payment.models import Payment  # noqa: F401
from app.property.models import Property
from app.reservation.models import Reservation  # noqa: F401
from app.review.models import Review  # noqa: F401

DATABASE_URL = "postgresql+asyncpg://bookit:bookit@localhost:5432/bookit"
API_BASE_URL = "http://localhost:8000"
DEMO_EMAIL_SUFFIX = "_concurrent_verify@example.com"
CONCURRENT_REQUESTS = 20


async def setup_test_property(engine: AsyncEngine) -> tuple[UUID, UUID]:
    """Create a host, a guest, and one property directly via the DB -- a real
    signup/login/POST-properties round trip isn't the point of this script,
    same shortcut verify_no_overlapping_reservations.py takes. Returns
    (guest_id, property_id).
    """
    async with AsyncSession(engine) as session:
        host = User(email=f"host{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        guest = User(email=f"guest{DEMO_EMAIL_SUFFIX}", hashed_password="x")
        session.add_all([host, guest])
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
        # captured before commit() expires every attribute below -- accessing
        # prop.id afterward would need an async reload outside the greenlet
        # context commit() leaves us in (docs/db/idempotency-basics.md's
        # flush()-vs-commit() distinction, same shape here)
        guest_id, property_id = guest.id, prop.id
        await session.commit()

    print(f"Created property {property_id}")
    return guest_id, property_id


async def fire_concurrent_bookings(guest_id: UUID, property_id: UUID) -> list[httpx.Response]:
    """Fire CONCURRENT_REQUESTS genuinely concurrent POST /reservations for the
    same property and identical dates, as one guest -- guest identity plays no
    part in the EXCLUDE constraint being tested (property_id + date overlap
    only), so one token for all requests is enough; see the chat discussion
    this script came out of for why that doesn't weaken the test.
    """
    # throwaway sid (BOOK-78): access tokens aren't checked against the refresh
    # store, so this needs no session in Redis -- it just has to be well-formed
    token = create_access_token(guest_id, new_id())
    headers = {"Authorization": f"Bearer {token}"}
    reservation_body = {
        "property_id": str(property_id),
        "check_in": "2026-10-01",
        "check_out": "2026-10-05",
        "guest_count": 2,
    }

    async with httpx.AsyncClient(base_url=API_BASE_URL, timeout=30.0) as client:

        async def attempt_booking() -> httpx.Response:
            # a distinct Idempotency-Key per attempt -- these are CONCURRENT_REQUESTS
            # separate booking attempts racing for the same dates, not retries of
            # one attempt (docs/db/idempotency-basics.md's "one key per click").
            # Reusing one key here would test BOOK-36's key-collision protection
            # instead of BOOK-35's date-overlap protection, since the idempotency
            # check runs before the overlap check ever does.
            return await client.post(
                "/reservations",
                json=reservation_body,
                headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
            )

        print(f"Firing {CONCURRENT_REQUESTS} concurrent POST /reservations ...")
        return await asyncio.gather(*(attempt_booking() for _ in range(CONCURRENT_REQUESTS)))


def report_results(responses: list[httpx.Response]) -> bool:
    """Print the outcome and return whether it matches what BOOK-35 promises:
    exactly one success, everything else a clean conflict, nothing else.
    """
    status_counts = Counter(r.status_code for r in responses)
    print(f"Status codes: {dict(status_counts)}")

    successes = status_counts.get(201, 0)
    conflicts = status_counts.get(409, 0)
    unexpected = sum(count for code, count in status_counts.items() if code not in (201, 409))
    passed = successes == 1 and conflicts == CONCURRENT_REQUESTS - 1 and unexpected == 0

    if passed:
        print(f"PASS -- exactly 1 succeeded, {conflicts} got a clean 409, 0 unexpected")
    else:
        print(
            f"FAIL -- expected exactly 1x 201 and {CONCURRENT_REQUESTS - 1}x 409, "
            f"got {successes}x 201, {conflicts}x 409, {unexpected} unexpected"
        )
    return passed


async def cleanup(engine: AsyncEngine, property_id: UUID) -> None:
    async with engine.begin() as conn:
        # idempotency_keys.reservation_id FKs to reservations.id -- must go first
        await conn.execute(
            text(
                "DELETE FROM idempotency_keys WHERE reservation_id IN "
                "(SELECT id FROM reservations WHERE property_id = :id)"
            ),
            {"id": property_id},
        )
        await conn.execute(
            text("DELETE FROM reservations WHERE property_id = :id"), {"id": property_id}
        )
        await conn.execute(text("DELETE FROM properties WHERE id = :id"), {"id": property_id})
        await conn.execute(text(f"DELETE FROM users WHERE email LIKE '%{DEMO_EMAIL_SUFFIX}'"))
    print("cleaned up")


async def main() -> None:
    engine = create_async_engine(DATABASE_URL)
    guest_id, property_id = await setup_test_property(engine)
    try:
        responses = await fire_concurrent_bookings(guest_id, property_id)
        passed = report_results(responses)
    finally:
        await cleanup(engine, property_id)
        await engine.dispose()

    if not passed:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
