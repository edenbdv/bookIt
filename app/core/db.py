from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.core.config import get_settings


# shared db connection
@lru_cache
def get_engine() -> AsyncEngine:
    return create_async_engine(get_settings().database_url)
