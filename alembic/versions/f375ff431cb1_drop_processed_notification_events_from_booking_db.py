"""drop processed_notification_events from Booking's database

Revision ID: f375ff431cb1
Revises: 1db36abe6069
Create Date: 2026-10-19 23:00:00.000000

BOOK-64 step 4c-4 -- the contract phase. Notification's Inbox now lives in
its own `notifications` database (app/notification/migrations), and the
consumer reads and writes only there. This copy in Booking's database has no
reader or writer left (confirmed live: it stopped changing after the switch).

Its rows are not copied anywhere: decision 2-A, the new Inbox started empty
(microservices-decisions.md §8). In a real deployment you'd copy them into
Notification's database before this runs.

downgrade() recreates it as it stood after 1db36abe6069 -- primary key, no
foreign key -- so the older migrations' downgrades still find it.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f375ff431cb1"
down_revision: str | Sequence[str] | None = "1db36abe6069"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_table("processed_notification_events")


def downgrade() -> None:
    """Downgrade schema."""
    op.create_table(
        "processed_notification_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("event_id"),
    )
