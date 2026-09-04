import json
import logging
from typing import Any

from app.core.cache.redis_client import get_redis_client

logger = logging.getLogger(__name__)


async def cache_get(key: str) -> Any | None:
    """Read a JSON-deserialized value from Redis.

    Returns None on a miss OR if Redis is unreachable -- the caller can't
    tell the difference and shouldn't need to: both mean "go compute it
    yourself." Caching is an optimization, never a dependency.
    """
    try:
        raw = await get_redis_client().get(key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_get(%s) failed, degrading to a miss: %s", key, exc)
        return None
    if raw is None:
        return None
    return json.loads(raw)


async def cache_set(key: str, value: Any, ttl_seconds: int) -> None:
    """Write a JSON-serialized value to Redis with a TTL. Failure is swallowed (logged)."""
    try:
        await get_redis_client().set(key, json.dumps(value), ex=ttl_seconds)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cache_set(%s) failed, continuing without caching it: %s", key, exc)
