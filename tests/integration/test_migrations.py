"""BOOK-61 smoke test: `alembic upgrade head` on an empty container builds the
schema production runs -- including the parts no model can describe
(testing-basics.md §3.3)."""

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.anyio
async def test_migrations_reach_head(db_engine: AsyncEngine) -> None:
    head = ScriptDirectory.from_config(Config(str(REPO_ROOT / "alembic.ini"))).get_current_head()
    async with db_engine.connect() as conn:
        current = await conn.scalar(text("SELECT version_num FROM alembic_version"))
    assert current == head


# btree_gist is enabled only in migration 23ec21d8edac -- the exact thing
# create_all would have silently skipped
@pytest.mark.anyio
async def test_btree_gist_extension_installed(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        installed = await conn.scalar(
            text("SELECT 1 FROM pg_extension WHERE extname = 'btree_gist'")
        )
    assert installed == 1


# the double-booking guard itself (BOOK-24) -- added by raw SQL in migration
# 8d5c38056826, depends on btree_gist above
@pytest.mark.anyio
async def test_no_overlapping_reservations_constraint_exists(db_engine: AsyncEngine) -> None:
    async with db_engine.connect() as conn:
        constraint_type = await conn.scalar(
            # ::text -- contype is Postgres's internal "char" type, which
            # asyncpg hands back as bytes (b'x'), not str
            text(
                "SELECT contype::text FROM pg_constraint"
                " WHERE conname = 'no_overlapping_reservations'"
            )
        )
    assert constraint_type == "x"  # 'x' = exclusion constraint


# BOOK-64 step 4: Notification's Inbox lives only in its own database --
# Booking's migrations dropped their copy (f375ff431cb1), Notification's own
# migrations (app/notification/migrations) created it in `notifications`
@pytest.mark.anyio
async def test_inbox_table_lives_only_in_notifications_db(
    db_engine: AsyncEngine, notification_db_engine: AsyncEngine
) -> None:
    query = text("SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename")
    async with db_engine.connect() as conn:
        booking_tables = set((await conn.scalars(query)).all())
    async with notification_db_engine.connect() as conn:
        notification_tables = set((await conn.scalars(query)).all())

    assert "processed_notification_events" not in booking_tables
    assert notification_tables == {"processed_notification_events", "notification_alembic_version"}
