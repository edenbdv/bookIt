import time
import uuid
from dataclasses import dataclass

from app.core.redis_client import get_redis_client


@dataclass
class RateLimitResult:
    allowed: bool
    limit: int
    remaining_requests: int
    retry_after_seconds: int


async def check_fixed_window(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """INCR/EXPIRE fixed-window check -- see docs/cache/rate-limiting-basics.md.

    Unlike cache_get/cache_set (core/cache.py), this does NOT catch Redis
    errors itself: whether a Redis outage should fail open or fail closed
    differs by endpoint group (see that doc's "Decision" table), so the
    caller decides -- this primitive just lets the error propagate.
    """
    redis = get_redis_client()
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, window_seconds)

    allowed = current <= limit
    retry_after_seconds = window_seconds
    if not allowed:
        ttl = await redis.ttl(key)
        retry_after_seconds = ttl if ttl > 0 else window_seconds

    return RateLimitResult(
        allowed=allowed,
        limit=limit,
        remaining_requests=max(0, limit - current),
        retry_after_seconds=retry_after_seconds,
    )


async def check_sliding_window(key: str, limit: int, window_seconds: int) -> RateLimitResult:
    """Sorted-set sliding-window-log check -- see docs/cache/rate-limiting-basics.md.

    Logs this request's timestamp, drops entries older than window_seconds,
    then counts what's left. Exact over the trailing window (no fixed-window
    boundary burst), at the cost of one entry per request instead of one
    integer -- see that doc's algorithm comparison for why this is only
    used on /auth/login.

    Same error-propagation contract as check_fixed_window: Redis errors are
    not caught here, the caller decides fail open vs. fail closed.
    """
    redis = get_redis_client()
    now = time.time()
    window_start = now - window_seconds

    # unique member per request -- ZADD would overwrite, not add, if two
    # requests in the same millisecond shared a member string
    await redis.zadd(key, {f"{now}-{uuid.uuid4().hex}": now})
    await redis.zremrangebyscore(key, "-inf", window_start)
    count = await redis.zcard(key)
    await redis.expire(key, window_seconds)

    allowed = count <= limit
    retry_after_seconds = window_seconds
    if not allowed:
        oldest = await redis.zrange(key, 0, 0, withscores=True)
        if oldest:
            oldest_score = oldest[0][1]
            retry_after_seconds = max(1, int(window_seconds - (now - oldest_score)))

    return RateLimitResult(
        allowed=allowed,
        limit=limit,
        remaining_requests=max(0, limit - count),
        retry_after_seconds=retry_after_seconds,
    )
