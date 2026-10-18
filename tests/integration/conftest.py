"""BOOK-61: integration-test fixtures -- a real Postgres, not FakeUnitOfWork.

Only tests under tests/integration/ ever request these, so the unit tests
elsewhere in tests/ still run without Docker. Concepts behind every choice
here: docs/testing/testing-basics.md (§ refs below).
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer

# imported for their side effect, same list as alembic/env.py: every model
# must be registered before any one of them is used, or relationship()
# targets like Reservation.payments can't resolve
import app.auth.models
import app.core.idempotency.models
import app.core.outbox.models
import app.notification.models
import app.payment.models
import app.property.models
import app.reservation.models
import app.review.models  # noqa: F401
from alembic import command
from app.core.cache.redis_client import get_redis_client
from app.core.config import get_settings
from app.core.db.engine import get_engine, get_session_factory
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parents[2]

# same images as docker-compose.yml's `db` / `redis` services -- tests run
# against the versions the app actually runs on
POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"


def _clear_cached_clients() -> None:
    # all four are @lru_cache'd singletons (core/config.py, core/db/engine.py,
    # core/cache/redis_client.py) -- without clearing them, anything already
    # built against the dev DB/Redis URL keeps being reused, test container
    # or not (testing-basics.md §3.6)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_redis_client.cache_clear()


# scope="session": starting a container + running every migration takes
# seconds, so once per test run, not once per test (testing-basics.md §3.2).
# Sync on purpose -- alembic/env.py calls asyncio.run() itself, which can't
# run inside an already-running event loop.
@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    with PostgresContainer(POSTGRES_IMAGE, driver="asyncpg") as postgres:
        url = postgres.get_connection_url()
        with pytest.MonkeyPatch.context() as mp:
            # pydantic-settings: a real env var beats .env, so this wins over
            # whatever DATABASE_URL the developer's .env points at
            mp.setenv("DATABASE_URL", url)
            _clear_cached_clients()

            # real migrations, not Base.metadata.create_all() -- btree_gist
            # only exists in a migration (testing-basics.md §3.3)
            command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")

            yield url

        # env var restored by the context manager; drop anything cached
        # against the now-gone container too
        _clear_cached_clients()


# same idea as postgres_url, for Redis: rate limiting, refresh-token storage
# and cache-aside all sit in real request paths (testing-basics.md §3.6).
# Not autouse -- only tests that need Redis pay for starting it.
@pytest.fixture(scope="session")
def redis_url() -> Iterator[str]:
    with RedisContainer(REDIS_IMAGE) as redis:
        host = redis.get_container_host_ip()
        port = redis.get_exposed_port(6379)
        url = f"redis://{host}:{port}/0"
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("REDIS_URL", url)
            _clear_cached_clients()
            yield url
        _clear_cached_clients()


# scope="function": a fresh engine per test, disposed at the end -- keeps the
# engine's lifetime the same as the per-test event loop, so no connection
# outlives the loop it was opened on (testing-basics.md §3.5)
@pytest.fixture
async def db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url)
    yield engine
    await engine.dispose()


# autouse: every integration test gets this without asking for it -- one
# forgotten fixture would otherwise leak rows into whichever test runs next.
# TRUNCATE, not rollback-per-test: app code commits on its own on purpose
# (e.g. BOOK-55's commit split), which would end a wrapping transaction
# early (testing-basics.md §3.4). Table list read from the catalog rather
# than hardcoded, so a table added by a future migration is covered too.
@pytest.fixture(autouse=True)
async def _truncate_tables(db_engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    async with db_engine.begin() as conn:
        tables = (
            await conn.scalars(
                text(
                    "SELECT quote_ident(tablename) FROM pg_tables"
                    " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
                )
            )
        ).all()
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    # expire_on_commit=False -- same as the app's own session factory
    # (core/db/engine.py), so objects stay readable after a commit
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        yield session


# Real HTTP into the real app, in-process -- no server, no port
# (testing-basics.md §3.6). No dependency_overrides: the app's own cached
# settings/engine/Redis client are pointed at the containers instead, so no
# app code is swapped out.
#
# Per test, on both ends: clearing the caches up front makes the app build a
# fresh engine + Redis client on *this* test's event loop; disposing/closing
# them afterwards stops either outliving that loop (testing-basics.md §3.5).
@pytest.fixture
async def api_client(postgres_url: str, redis_url: str) -> AsyncIterator[AsyncClient]:
    _clear_cached_clients()
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    await get_engine().dispose()
    # Redis's counterpart to _truncate_tables -- otherwise one test's
    # rate-limit counters and refresh tokens leak into the next. Through the
    # app's own client, before closing it: reuses its pooled connection
    # instead of opening a new one just to flush.
    redis = get_redis_client()
    await redis.flushdb()
    await redis.aclose()
    _clear_cached_clients()
