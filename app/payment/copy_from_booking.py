"""BOOK-65: copy Payment's rows from Booking's database into Payment's own.
Run with:

    uv run python -m app.payment.copy_from_booking

Temporary -- only exists while Payment moves out; deleted at the contract step,
together with the old tables (microservices-decisions.md §18).

Safe to repeat: every insert is ON CONFLICT DO NOTHING, so already-copied rows are
skipped. Run it once now, and a final time at the switch, with app / worker /
payment-consumer stopped so nothing is written in between.
"""

import asyncio
import logging

import asyncpg
from sqlalchemy.engine import make_url

from app.core.config import get_settings

logger = logging.getLogger(__name__)

# (source query in `bookit`, target table in `payments`, columns, conflict column).
# Order matters: payments before payment_idempotency_keys (its FK).
_COPIES = [
    (
        "SELECT id, reservation_id, kind, amount_cents, status, created_at FROM payments",
        "payments",
        ["id", "reservation_id", "kind", "amount_cents", "status", "created_at"],
        "id",
    ),
    (
        # only the payment half of the shared table -- reservation keys stay in Booking
        (
            "SELECT key, status::text, payment_id, created_at, expires_at "
            "FROM idempotency_keys WHERE payment_id IS NOT NULL"
        ),
        "payment_idempotency_keys",
        ["key", "status", "payment_id", "created_at", "expires_at"],
        "key",
    ),
    (
        # including unpublished rows -- Payment's relay sends them after the switch
        (
            "SELECT id, event_type, reservation_id, payload, created_at, published_at "
            "FROM payment_outbox_events"
        ),
        "payment_outbox_events",
        ["id", "event_type", "reservation_id", "payload", "created_at", "published_at"],
        "id",
    ),
    (
        "SELECT event_id, processed_at FROM processed_booking_events",
        "processed_booking_events",
        ["event_id", "processed_at"],
        "event_id",
    ),
]


def _asyncpg_url(sqlalchemy_url: str) -> str:
    # settings hold "postgresql+asyncpg://..." -- asyncpg wants plain "postgresql://"
    return (
        make_url(sqlalchemy_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )


async def copy_from_booking() -> dict[str, int]:
    """Copy every row missing from Payment's database. Returns rows inserted per table."""
    settings = get_settings()
    source = await asyncpg.connect(_asyncpg_url(settings.database_url))
    target = await asyncpg.connect(_asyncpg_url(settings.payment_database_url))
    inserted: dict[str, int] = {}
    try:
        async with target.transaction():  # all tables or none
            for query, table, columns, conflict in _COPIES:
                rows = await source.fetch(query)
                placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
                before = await target.fetchval(f"SELECT count(*) FROM {table}")
                await target.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders}) "
                    f"ON CONFLICT ({conflict}) DO NOTHING",
                    [tuple(row) for row in rows],
                )
                after = await target.fetchval(f"SELECT count(*) FROM {table}")
                inserted[table] = after - before
                logger.info("%s: %d in Booking, %d newly copied", table, len(rows), after - before)
    finally:
        await source.close()
        await target.close()
    return inserted


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(copy_from_booking())


if __name__ == "__main__":
    main()
