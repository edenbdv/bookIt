"""add booking_payment_rejected outbox event type

Revision ID: 9b3e1c7d2a40
Revises: 1af8dfcfcd81
Create Date: 2026-10-23 12:00:00.000000

BOOK-65: a new value on the outboxeventtype Postgres enum -- Booking's
"I can't use this payment, refund it" event, for a charge that succeeded
after its reservation was already cancelled (epic-11-flow.md).

Same sync_enum_values approach as 54a6065ffb8e, for the same reason: it's
the only way to give an enum-value addition a working downgrade.
"""

from collections.abc import Sequence

from alembic_postgresql_enum import TableReference

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9b3e1c7d2a40"
down_revision: str | Sequence[str] | None = "1af8dfcfcd81"
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
        new_values=[
            "booking_created",
            "booking_confirmed",
            "booking_cancelled",
            "booking_payment_rejected",
        ],
        affected_columns=[_OUTBOX_EVENT_TYPE],
        enum_values_to_rename=[],
    )


def downgrade() -> None:
    """Downgrade schema."""
    # rows of the removed type would block the type swap. No Inbox cleanup
    # needed, unlike 54a6065ffb8e: nothing has an FK into outbox_events anymore
    op.execute("DELETE FROM outbox_events WHERE event_type = 'booking_payment_rejected'")
    op.sync_enum_values(
        enum_schema="public",
        enum_name="outboxeventtype",
        new_values=["booking_created", "booking_confirmed", "booking_cancelled"],
        affected_columns=[_OUTBOX_EVENT_TYPE],
        enum_values_to_rename=[],
    )
