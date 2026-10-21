"""create processed_notification_events

Revision ID: 39077418b1a2
Revises:
Create Date: 2026-10-19 16:00:00.000000

BOOK-64 step 4c: the first migration in Notification's own database -- its
Inbox table, the only table it owns. Same shape as the copy in Booking's
database after 4a (event_id as the primary key, no foreign key), which is
dropped from there once Notification has switched over (step 4c-4).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "39077418b1a2"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
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


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("processed_notification_events")
