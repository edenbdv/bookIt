"""Manual verification for the get_global_mean_rating cache-aside (BOOK-40).

Not a pytest test -- same reasoning as verify_idempotency_keys.py: there's
no test suite in this project yet (BOOK-61/62 for the planned real thing).
This runs real requests through UnitOfWork/service.py against the
dockerized dev Postgres + Redis and prints what actually happened, so this
ticket's behavior can be re-checked by hand later without re-deriving it
from scratch.

Requires: `docker compose up -d db redis` already running.
Run with: uv run python scripts/verify_cache_global_mean_rating.py
"""

import asyncio
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from redis.asyncio import Redis

from app.core.cache import cache_get, cache_set
from app.core.config import get_settings
from app.core.redis_client import get_redis_client
from app.core.unit_of_work import UnitOfWork
from app.property.service import GLOBAL_MEAN_RATING_CACHE_KEY, _get_global_mean_rating


async def main() -> None:
    redis = get_redis_client()
    ttl_setting = get_settings().global_mean_rating_cache_ttl_seconds

    # start from a clean slate -- a leftover key from a previous run would
    # make step 1 look like a "hit" when it should be a miss
    await redis.delete(GLOBAL_MEAN_RATING_CACHE_KEY)

    # 1. First call, cold cache -- must compute from the DB and populate Redis
    async with UnitOfWork() as uow:
        real_value = await _get_global_mean_rating(uow)
    raw = await redis.get(GLOBAL_MEAN_RATING_CACHE_KEY)
    ttl = await redis.ttl(GLOBAL_MEAN_RATING_CACHE_KEY)
    ok = raw is not None and 0 < ttl <= ttl_setting
    print(
        f"1. Cold cache -- computed {real_value}, Redis now holds {raw!r} with ttl={ttl}s -- "
        f"{'OK' if ok else 'UH OH, key missing or ttl out of range'}"
    )

    # 2. Poison the cache with a value that could never come from the DB, then
    #    call again -- if this returns the poisoned value, the read really
    #    came from Redis and not a silent recompute
    await cache_set(GLOBAL_MEAN_RATING_CACHE_KEY, 999.0, ttl_setting)
    async with UnitOfWork() as uow:
        poisoned_read = await _get_global_mean_rating(uow)
    ok = poisoned_read == 999.0
    print(
        f"2. Poisoned cache read -- got {poisoned_read} -- "
        f"{'OK, served from cache' if ok else 'UH OH, recomputed instead of reading cache'}"
    )

    # 3. Clear the poisoned key -- a fresh miss must recover the real value,
    #    not stay stuck on the poisoned one
    await redis.delete(GLOBAL_MEAN_RATING_CACHE_KEY)
    async with UnitOfWork() as uow:
        recovered_value = await _get_global_mean_rating(uow)
    ok = recovered_value == real_value
    print(
        f"3. Fresh miss after clearing -- recomputed {recovered_value} -- "
        f"{'OK, matches step 1' if ok else 'UH OH, does not match the real value'}"
    )

    # 4. Redis unreachable -- must degrade to a direct DB read, never raise.
    #    Nothing listens on this port, so the connection fails fast.
    broken_client = Redis.from_url("redis://localhost:1/0")
    with patch("app.core.cache.get_redis_client", return_value=broken_client):
        miss = await cache_get(GLOBAL_MEAN_RATING_CACHE_KEY)
        print(
            f"4a. cache_get with Redis unreachable -- returned {miss!r} -- "
            f"{'OK, degraded to a miss' if miss is None else 'UH OH, should be None'}"
        )
        try:
            async with UnitOfWork() as uow:
                degraded_value = await _get_global_mean_rating(uow)
            print(
                f"4b. get_global_mean_rating with Redis unreachable -- returned "
                f"{degraded_value} without raising -- OK"
            )
        except Exception as exc:  # noqa: BLE001
            print(f"4b. get_global_mean_rating with Redis unreachable -- UH OH, raised {exc!r}")
    await broken_client.aclose()

    # cleanup -- no Postgres rows were created, only this one Redis key
    await redis.delete(GLOBAL_MEAN_RATING_CACHE_KEY)
    print("cleaned up")


if __name__ == "__main__":
    asyncio.run(main())
