import asyncio
import functools
from collections.abc import AsyncGenerator, Awaitable, Callable
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


def async_task[T](async_fn: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    """Bridges an async function into a sync call via asyncio.run().

    Disposes the cached engine after each call -- otherwise a second
    asyncio.run() opens a new event loop while the pool's connections are
    still bound to the first (now-closed) one, and asyncpg errors.
    """

    @functools.wraps(async_fn)
    async def run_and_dispose(*args: object, **kwargs: object) -> T:
        try:
            return await async_fn(*args, **kwargs)
        finally:
            await get_engine().dispose()

    @functools.wraps(async_fn)
    def wrapper(*args: object, **kwargs: object) -> T:
        return asyncio.run(run_and_dispose(*args, **kwargs))

    return wrapper


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
