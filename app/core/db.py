from collections.abc import AsyncGenerator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings


# shared db connection
@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url)


@lru_cache
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False)


# FastAPI dependency: yields a request-scoped session, closed once the
# request ends (see docs/db/dependency-injection-basics.md for why there's
# no visible .close() call, and why closing it isn't the same as
# closing the underlying connection).
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    async with get_session_factory()() as session:
        yield session
