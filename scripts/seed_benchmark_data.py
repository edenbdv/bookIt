"""Bulk realistic-volume data for BOOK-88's measurement spike.

app/core/seed.py's handful of fixed rows is deliberately tiny and readable --
fine for exercising code paths, useless for measuring query cost, since
everything fits in a single Postgres page and every plan looks instant.
This script exists purely to make BOOK-88's EXPLAIN ANALYZE / endpoint
latency numbers mean something: ~1000 properties, ~50 reservations (and one
guest review each) per property, ~50k reviews total, one photo per property.

Idempotent like app/core/seed.py, but via a different mechanism: individual
get_or_create() round-trips don't scale to tens of thousands of rows, so
every id here is a deterministic uuid5 (same input -> same id every run) and
inserts use ON CONFLICT DO NOTHING instead of a SELECT-then-insert per row.

Requires: `docker compose up -d db` already running.
Run with: uv run python scripts/seed_benchmark_data.py
"""

import asyncio
import random
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import create_async_engine

from app.auth.models import User, UserRole
from app.property.models import Property, PropertyPhoto
from app.reservation.models import Reservation, ReservationStatus
from app.review.models import Review

DATABASE_URL = "postgresql+asyncpg://bookit:bookit@localhost:5432/bookit"

# a fixed namespace so uuid5(NAMESPACE, "<anything>") is stable across runs --
# same technique as app/core/seed.py's hardcoded UUIDs, just generated instead
# of hand-typed since there are tens of thousands of them here
NAMESPACE = uuid.UUID("11111111-1111-1111-1111-111111111111")


def det_id(*parts: str) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, ":".join(parts))


NUM_HOSTS = 50
NUM_GUESTS = 300
NUM_PROPERTIES = 1000
RESERVATIONS_PER_PROPERTY = 50  # -> ~50,000 reservations, ~50,000 reviews

CITIES = [
    "Tel Aviv",
    "Jerusalem",
    "Haifa",
    "Eilat",
    "Netanya",
    "Herzliya",
    "Ra'anana",
    "Beer Sheva",
    "Ashdod",
    "Tiberias",
]

# BOOK-88's holiday-clustering point: real booking dates aren't spread evenly
# across the year, they bunch up around a handful of periods (summer break,
# the Rosh Hashanah-through-Simchat Torah stretch). Modeling that here matters
# for two different reasons: it gives the availability NOT EXISTS subquery a
# realistic density of overlapping reservations to chew on instead of sparse
# noise, and -- more importantly for BOOK-88's hit-rate estimate -- it means
# date-range searches have a real reason to collide on the same cache key
# instead of every visitor picking a uniformly random, never-repeated range.
HOT_WINDOWS = [
    (date(2026, 7, 1), date(2026, 8, 31)),  # summer break
    (date(2026, 9, 11), date(2026, 10, 4)),  # Rosh Hashanah -> Simchat Torah
]


def in_hot_window(d: date) -> bool:
    return any(start <= d <= end for start, end in HOT_WINDOWS)


def build_reservations_for_property(property_id: uuid.UUID, guest_ids: list[uuid.UUID]):
    """Sequential, non-overlapping stays -- required by the EXCLUDE constraint,
    same as any real booking history. The gap between stays shrinks inside a
    hot window and widens outside one, which is what makes bookings cluster
    there instead of spreading uniformly across the year.
    """
    reservations = []
    reviews = []
    cursor = date(2026, 1, 1) + timedelta(days=random.randint(0, 20))
    for i in range(RESERVATIONS_PER_PROPERTY):
        stay_nights = random.randint(2, 6)
        check_in = cursor
        check_out = check_in + timedelta(days=stay_nights)
        guest_id = random.choice(guest_ids)
        reservation_id = det_id("reservation", str(property_id), str(i))

        reservations.append(
            {
                "id": reservation_id,
                "property_id": property_id,
                "guest_id": guest_id,
                "check_in": check_in,
                "check_out": check_out,
                "guest_count": random.randint(1, 4),
                "total_cents": stay_nights * random.randint(15000, 60000),
                "status": ReservationStatus.COMPLETED,
            }
        )
        reviews.append(
            {
                "id": det_id("review", str(property_id), str(i)),
                "reservation_id": reservation_id,
                "author_id": guest_id,
                "rating": random.randint(1, 5),
                "comment": "Generated for BOOK-88 benchmark data.",
            }
        )

        gap_days = random.randint(1, 3) if in_hot_window(cursor) else random.randint(4, 12)
        cursor = check_out + timedelta(days=gap_days)

    return reservations, reviews


async def main() -> None:
    engine = create_async_engine(DATABASE_URL)

    hosts = [
        {
            "id": det_id("host", str(i)),
            "email": f"bench_host_{i}@example.com",
            "display_name": f"Bench Host {i}",
            "hashed_password": "seed-data-not-a-real-hash",
            "role": UserRole.USER,
        }
        for i in range(NUM_HOSTS)
    ]
    guests = [
        {
            "id": det_id("guest", str(i)),
            "email": f"bench_guest_{i}@example.com",
            "display_name": f"Bench Guest {i}",
            "hashed_password": "seed-data-not-a-real-hash",
            "role": UserRole.USER,
        }
        for i in range(NUM_GUESTS)
    ]
    host_ids = [h["id"] for h in hosts]
    guest_ids = [g["id"] for g in guests]

    properties = []
    photos = []
    reservations: list[dict] = []
    reviews: list[dict] = []

    for i in range(NUM_PROPERTIES):
        property_id = det_id("property", str(i))
        properties.append(
            {
                "id": property_id,
                "host_id": random.choice(host_ids),
                "title": f"Benchmark listing #{i}",
                "description": "Generated for BOOK-88 benchmark data.",
                "city": random.choice(CITIES),
                "country": "IL",
                "max_guests": random.randint(1, 8),
                "price_per_night_cents": random.randint(15000, 90000),
            }
        )
        photos.append(
            {
                "id": det_id("photo", str(i)),
                "property_id": property_id,
                "url": f"https://picsum.photos/seed/{property_id}/800/600",
                "position": 0,
            }
        )
        prop_reservations, prop_reviews = build_reservations_for_property(property_id, guest_ids)
        reservations.extend(prop_reservations)
        reviews.extend(prop_reviews)

    async with engine.begin() as conn:
        # ON CONFLICT DO NOTHING, not get_or_create: at this row count, a
        # SELECT-then-insert round trip per row would dominate the run time.
        # Deterministic ids (det_id) are what makes this safe to re-run --
        # a repeat run tries to insert the exact same rows and no-ops on all
        # of them, rather than silently duplicating.
        await conn.execute(insert(User).on_conflict_do_nothing(), hosts + guests)
        await conn.execute(insert(Property).on_conflict_do_nothing(), properties)
        await conn.execute(insert(PropertyPhoto).on_conflict_do_nothing(), photos)
        # chunked: one 50k-row statement is unnecessarily memory-heavy on the
        # driver side for no real benefit over a handful of round trips
        for start in range(0, len(reservations), 5000):
            await conn.execute(
                insert(Reservation).on_conflict_do_nothing(), reservations[start : start + 5000]
            )
        for start in range(0, len(reviews), 5000):
            await conn.execute(
                insert(Review).on_conflict_do_nothing(), reviews[start : start + 5000]
            )

    await engine.dispose()
    print(
        f"Seeded (or already present): {len(hosts)} hosts, {len(guests)} guests, "
        f"{len(properties)} properties, {len(photos)} photos, "
        f"{len(reservations)} reservations, {len(reviews)} reviews."
    )


if __name__ == "__main__":
    asyncio.run(main())
