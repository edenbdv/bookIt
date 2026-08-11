"""enable btree_gist extension

Revision ID: 23ec21d8edac
Revises: c61dc204332f
Create Date: 2026-08-11 19:13:33.270077

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "23ec21d8edac"
down_revision: str | Sequence[str] | None = "c61dc204332f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    # Needed so a single EXCLUDE USING gist constraint can mix plain equality
    # (property_id WITH =) with a GiST-native operator (daterange WITH &&).
    # GiST has no built-in support for = on ordinary scalar types; btree_gist
    # adds GiST-compatible versions of the standard B-tree operators.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP EXTENSION IF EXISTS btree_gist")
