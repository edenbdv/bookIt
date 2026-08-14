"""prevent overlapping reservations

Revision ID: 8d5c38056826
Revises: 23ec21d8edac
Create Date: 2026-08-11 19:18:59.340156

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8d5c38056826"
down_revision: str | Sequence[str] | None = "23ec21d8edac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Alembic's autogenerate does not detect SQLAlchemy's ExcludeConstraint
    # (Postgres-specific, no generic op.create_exclude_constraint exists),
    # so this is hand-written raw SQL, not generated.
    #
    # DB-level constraint, not app-level checking: an app-level "is this
    # property free?" query can't stop two concurrent requests from both
    # passing the check before either writes -- only a database constraint
    # can make the invalid state impossible to create in the first place
    # (see docs/db/exclude-constraint-basics.md and docs/db/concurrency-basics.md).
    op.execute("""
        ALTER TABLE reservations ADD CONSTRAINT no_overlapping_reservations
        EXCLUDE USING gist (
            property_id WITH =,
            daterange(check_in, check_out, '[)') WITH &&
        ) WHERE (status <> 'cancelled')
        """)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("ALTER TABLE reservations DROP CONSTRAINT no_overlapping_reservations")
