"""Manual verification for the no_overlapping_reservations EXCLUDE constraint (BOOK-24).

Not a pytest test -- there's no test suite in this project yet (see BOOK-61/BOOK-62
for the planned real thing: testcontainers-based integration tests and a proper
booking-concurrency contract test). This just runs real inserts against the
dockerized dev Postgres and prints what actually happened, so the constraint's
behavior can be re-checked by hand without re-deriving it from scratch.

Requires: `docker compose up -d db` already running.
Run with: uv run python scripts/verify_no_overlapping_reservations.py
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.auth.models import User
from app.payment.models import Payment  # noqa: F401
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from app.review.models import Review  # noqa: F401

DATABASE_URL = "postgresql+asyncpg://bookit:bookit@localhost:5432/bookit"
DEMO_EMAIL_SUFFIX = "_overlap_verify@example.com"


async def main() -> None:
    engine = create_async_engine(DATABASE_URL)

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
        print(f"Created property {prop.id}")

        # 1. Base reservation: Sep 1 - Sep 5, confirmed
        r1 = Reservation(
            property_id=prop.id,
            guest_id=guest.id,
            check_in=date(2026, 9, 1),
            check_out=date(2026, 9, 5),
            guest_count=2,
            total_cents=40000,
            status=ReservationStatus.CONFIRMED,
        )
        session.add(r1)
        await session.flush()
        print("Reservation Sep1-Sep5 (confirmed) -- OK")

        # 2. Overlapping reservation: Sep 3 - Sep 7 -- MUST be rejected
        r2 = Reservation(
            property_id=prop.id,
            guest_id=guest.id,
            check_in=date(2026, 9, 3),
            check_out=date(2026, 9, 7),
            guest_count=2,
            total_cents=40000,
            status=ReservationStatus.CONFIRMED,
        )
        session.add(r2)
        try:
            await session.flush()
            print("Overlapping reservation Sep3-Sep7 -- UH OH, should have failed")
        except IntegrityError as e:
            await session.rollback()
            print("Overlapping reservation Sep3-Sep7 -- REJECTED as expected:")
            print(f"  {str(e.orig).splitlines()[0]}")
            # re-attach objects lost to rollback
            session.add_all([host, guest, prop, r1])
            await session.flush()

        # 3. Back-to-back reservation: Sep 5 - Sep 8 (checkout of r1 == check-in) -- must succeed
        r3 = Reservation(
            property_id=prop.id,
            guest_id=guest.id,
            check_in=date(2026, 9, 5),
            check_out=date(2026, 9, 8),
            guest_count=2,
            total_cents=30000,
            status=ReservationStatus.CONFIRMED,
        )
        session.add(r3)
        await session.flush()
        print("Adjacent reservation Sep5-Sep8 (same-day turnover) -- OK, as expected")

        # 4. A CANCELLED reservation overlapping r1 -- must succeed (excluded from check)
        r4 = Reservation(
            property_id=prop.id,
            guest_id=guest.id,
            check_in=date(2026, 9, 2),
            check_out=date(2026, 9, 4),
            guest_count=2,
            total_cents=20000,
            status=ReservationStatus.CANCELLED,
        )
        session.add(r4)
        await session.flush()
        print("Overlapping but CANCELLED reservation Sep2-Sep4 -- OK, as expected")

    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM reservations"))
        await conn.execute(text("DELETE FROM properties"))
        await conn.execute(text(f"DELETE FROM users WHERE email LIKE '%{DEMO_EMAIL_SUFFIX}'"))
    await engine.dispose()
    print("cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
