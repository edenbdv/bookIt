"""EXPLAIN (ANALYZE, BUFFERS) against the real list_filtered query (BOOK-88).

Captures the exact Select statement PropertyRepository.list_filtered builds --
via SQLAlchemy's before_cursor_execute event during a real call, not a
hand-written approximation of it -- then compiles it with literal values
inlined and runs EXPLAIN (ANALYZE, BUFFERS) on that on a fresh connection.

Requires: `docker compose up -d db` running, and scripts/seed_benchmark_data.py
already run so the plan reflects realistic data volume, not a handful of rows.

Run with: uv run python scripts/explain_list_filtered.py

How this pinpointed the actual bottleneck (BOOK-88), not just "it's slow":
two separate signals, neither one a guess.

1. Compare `Execution Time` ACROSS the four cases below. If a heavily
   filtered search (e.g. city+guests+dates, ~37 matching properties) isn't
   meaningfully faster than an unfiltered one, the cost can't be coming from
   the filters -- it rules out an entire category of explanation before
   looking at a single plan node.
2. Within one plan, every node prints its OWN `actual time=..` (see
   docs/db/explain-analyze-basics.md) -- like a receipt itemized per line,
   not just a total. Reading off which nodes have the largest actual time
   is what identified _global_mean_rating()/_review_stats_subquery() as
   >90% of the cost, versus every actual filter (city/guests/availability)
   measuring under 2ms combined.
"""

import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

# imported for their side effect of registering with the ORM mapper registry
# -- Reservation's relationships reference "User"/"Payment"/"Review" as
# strings, which only resolve if those modules were actually imported
# somewhere, same reasoning as scripts/verify_no_overlapping_reservations.py
from app.auth.models import User  # noqa: F401
from app.payment.models import Payment  # noqa: F401
from app.property.repository import PropertyRepository
from app.review.models import Review  # noqa: F401

DATABASE_URL = "postgresql+asyncpg://bookit:bookit@localhost:5432/bookit"


async def explain(label: str, **filters: object) -> None:
    engine = create_async_engine(DATABASE_URL)
    captured = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if context.compiled is not None:
            captured.append(context.compiled.statement)

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    async with AsyncSession(engine) as session:
        repo = PropertyRepository(session)
        await repo.list_filtered(
            limit=20, offset=0, sort=filters.pop("sort", "recommended"), smoothing_m=5, **filters
        )
    event.remove(engine.sync_engine, "before_cursor_execute", capture)

    # literal_binds inlines actual values into the SQL text, sidestepping the
    # asyncpg driver's own ($1, $2, ...) paramstyle entirely -- simplest way
    # to hand this exact statement to a plain EXPLAIN
    compiled_sql = str(
        captured[-1].compile(dialect=engine.dialect, compile_kwargs={"literal_binds": True})
    )

    async with engine.connect() as conn:
        result = await conn.execute(text(f"EXPLAIN (ANALYZE, BUFFERS) {compiled_sql}"))
        plan = "\n".join(row[0] for row in result)
    await engine.dispose()

    print(f"\n{'=' * 10} {label} {'=' * 10}")
    print(plan)


async def main() -> None:
    await explain("no filters (default browse)")
    await explain("city + guests", city="Tel Aviv", guests=2)
    await explain(
        "availability only (check_in/check_out)",
        check_in=date(2026, 9, 15),
        check_out=date(2026, 9, 18),
    )
    await explain(
        "city + guests + availability (realistic full search)",
        city="Tel Aviv",
        guests=2,
        check_in=date(2026, 9, 15),
        check_out=date(2026, 9, 18),
    )


if __name__ == "__main__":
    asyncio.run(main())
