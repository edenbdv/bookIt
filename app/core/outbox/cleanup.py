"""BOOK-50's last step -- delete outbox rows that already published
successfully, once they're old enough nobody still needs them for
debugging. Kafka holds its own copy of every published event for its own
retention window (kafka-basics.md §7-8), so keeping a published row here
forever is redundant, not extra safety -- see BOOK-50's ticket description
for the full reasoning.

Separate module from relay.py on purpose: this is housekeeping, not
publishing -- a different responsibility, even though both operate on
`OutboxEvent`.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox.models import OutboxEvent

# long enough to debug a recent delivery issue, short enough that the table
# doesn't grow forever
OUTBOX_RETENTION_DAYS = 7


async def delete_published_events(uow: UnitOfWork) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=OUTBOX_RETENTION_DAYS)
    await uow.session.execute(
        delete(OutboxEvent).where(
            OutboxEvent.published_at.is_not(None), OutboxEvent.published_at < cutoff
        )
    )
