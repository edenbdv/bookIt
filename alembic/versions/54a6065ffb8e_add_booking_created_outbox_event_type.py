"""add booking_created outbox event type

Revision ID: 54a6065ffb8e
Revises: 148e5e0f4aa5
Create Date: 2026-10-19 12:00:00.000000

BOOK-64 step 3a: a new value on the outboxeventtype Postgres enum, for the
BookingCreated event that replaces the direct Celery trigger of the
"reservation received" email (microservices-decisions.md §4).

sync_enum_values (alembic-postgresql-enum, see alembic/env.py) rather than a
raw ALTER TYPE ... ADD VALUE: Postgres can add an enum value but has no
"drop value", so only the library's swap-the-type approach gives this
migration a working downgrade.
"""

from collections.abc import Sequence

from alembic_postgresql_enum import TableReference

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "54a6065ffb8e"
down_revision: str | Sequence[str] | None = "148e5e0f4aa5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTBOX_EVENT_TYPE = TableReference(
    table_schema="public", table_name="outbox_events", column_name="event_type"
)


def upgrade() -> None:
    """Upgrade schema."""
    op.sync_enum_values(
        enum_schema="public",
        enum_name="outboxeventtype",
        new_values=["booking_created", "booking_confirmed", "booking_cancelled"],
        affected_columns=[_OUTBOX_EVENT_TYPE],
        enum_values_to_rename=[],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # rows of the removed type would block the type swap -- and once the
    # value is gone, nothing could still publish or consume them anyway.
    # Inbox markers first: processed_notification_events.event_id has a
    # plain FK (no ON DELETE) into outbox_events.id
    op.execute(
        "DELETE FROM processed_notification_events WHERE event_id IN "
        "(SELECT id FROM outbox_events WHERE event_type = 'booking_created')"
    )
    op.execute("DELETE FROM outbox_events WHERE event_type = 'booking_created'")
    op.sync_enum_values(
        enum_schema="public",
        enum_name="outboxeventtype",
        new_values=["booking_confirmed", "booking_cancelled"],
        affected_columns=[_OUTBOX_EVENT_TYPE],
        enum_values_to_rename=[],
    )
