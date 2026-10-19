"""BOOK-64 step 4b: Notification's own database access.

Instead of the monolith's UnitOfWork -- which opens one session and builds a
repository for every Booking table (users, reservations, payments...) --
Notification gets its own engine, on its own setting, for the only table it
owns: processed_notification_events (its Inbox).

Until step 4c the setting falls back to the shared database_url, so nothing
changes in behavior; only the wiring does.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


def notification_database_url() -> str:
    settings = get_settings()
    return settings.notification_database_url or settings.database_url


# same lru_cache singleton shape as core/db/engine.py's get_engine()
@lru_cache
def get_notification_engine() -> AsyncEngine:
    return create_async_engine(notification_database_url())


@lru_cache
def get_notification_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_notification_engine(), expire_on_commit=False)


@asynccontextmanager
async def notification_session() -> AsyncIterator[AsyncSession]:
    """One transaction: commits on a clean exit, rolls back if anything
    raised -- the same contract UnitOfWork gave the dispatcher."""
    async with get_notification_session_factory()() as session:
        try:
            yield session
        except BaseException:
            await session.rollback()
            raise
        await session.commit()
