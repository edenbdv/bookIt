from functools import lru_cache

from redis.asyncio import Redis

from app.core.config import get_settings


# shared redis connection
@lru_cache
def get_redis_client() -> Redis:
    return Redis.from_url(get_settings().redis_url)
