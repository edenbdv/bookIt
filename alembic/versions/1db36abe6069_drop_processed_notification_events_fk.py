"""drop processed_notification_events -> outbox_events foreign key

Revision ID: 1db36abe6069
Revises: 54a6065ffb8e
Create Date: 2026-10-19 15:00:00.000000

BOOK-64 step 4a. Notification's Inbox table keeps event_id as its primary
key (that's the dedup), but stops pointing at Booking's outbox_events:

- the two tables are about to live in different databases, and a foreign
  key can't cross databases (microservices-decisions.md §8);
- dedup never needed it -- the event_id arrives in the event payload, and
  uniqueness comes from the primary key;
- it was also a live bug: the daily outbox cleanup (core/outbox/cleanup.py)
  deletes published events older than 7 days, and this foreign key (no ON
  DELETE) made that DELETE fail as soon as Notification had handled one.

The constraint was created unnamed (787856f3e380), so this is Postgres's
default name for it.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1db36abe6069"
down_revision: str | Sequence[str] | None = "54a6065ffb8e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FK_NAME = "processed_notification_events_event_id_fkey"


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_constraint(_FK_NAME, "processed_notification_events", type_="foreignkey")


def downgrade() -> None:
    """Downgrade schema."""
    # without the constraint, the cleanup job may have deleted outbox rows an
    # Inbox row still names -- those orphans would block re-adding it
    op.execute(
        "DELETE FROM processed_notification_events p WHERE NOT EXISTS "
        "(SELECT 1 FROM outbox_events o WHERE o.id = p.event_id)"
    )
    op.create_foreign_key(
        _FK_NAME, "processed_notification_events", "outbox_events", ["event_id"], ["id"]
    )
