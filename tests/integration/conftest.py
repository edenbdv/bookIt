"""BOOK-61: integration-test fixtures -- a real Postgres, not FakeUnitOfWork.

Only tests under tests/integration/ ever request these, so the unit tests
elsewhere in tests/ still run without Docker. Concepts behind every choice
here: docs/testing/testing-basics.md (§ refs below).
"""

import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from unittest.mock import patch

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from notification import migrate as notification_migrate
from notification.config import get_notification_settings
from notification.db import (
    get_notification_engine,
    get_notification_session_factory,
    notification_database_url,
)
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from testcontainers.community.kafka import KafkaContainer
from testcontainers.community.postgres import PostgresContainer
from testcontainers.community.redis import RedisContainer
from testcontainers.core.docker_client import DockerClient

# imported for their side effect, same list as alembic/env.py: every model
# must be registered before any one of them is used, or relationship()
# targets like Reservation.payments can't resolve
import app.auth.models
import app.core.idempotency.models
import app.core.outbox.models
import app.payment.models
import app.property.models
import app.reservation.models
import app.review.models  # noqa: F401
from alembic import command
from app.core.cache.redis_client import get_redis_client
from app.core.config import get_settings
from app.core.db.engine import get_engine, get_session_factory
from app.main import create_app
from app.payment import migrate as payment_migrate
from app.payment.db import get_payment_engine, get_payment_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]

# same images as docker-compose.yml's `db` / `redis` services -- tests run
# against the versions the app actually runs on
POSTGRES_IMAGE = "postgres:16-alpine"
REDIS_IMAGE = "redis:7-alpine"
# testcontainers' Kafka module drives Confluent's image (its startup scripts),
# not docker-compose's apache/kafka -- same broker, both run in KRaft mode
KAFKA_IMAGE = "confluentinc/cp-kafka:7.6.0"

# up to ~10s for Docker to publish a container's port (testing-decisions.md §10)
PORT_LOOKUP_ATTEMPTS = 20
PORT_LOOKUP_DELAY_S = 0.5


# Docker Desktop (WSL2) can report a container "running" before its port
# mapping is published -- most often on the first run after Docker restarts.
# testcontainers looks the port up once and raises ConnectionError, and that
# one lookup is behind every container we start: ryuk's 8080 (inside the
# first container's start()), Kafka's 9093, Postgres and Redis. Retrying the
# lookup covers all of them; retrying start() wouldn't -- ryuk's container
# has a fixed name, so a second start() clashes with the first.
# autouse + session scope: set up before postgres_url & co. start anything.
@pytest.fixture(scope="session", autouse=True)
def _retry_port_lookup() -> Iterator[None]:
    lookup = DockerClient.port

    def lookup_with_retry(self: DockerClient, container_id: str, port: int) -> str:
        for _ in range(PORT_LOOKUP_ATTEMPTS - 1):
            try:
                return lookup(self, container_id, port)
            except ConnectionError:
                time.sleep(PORT_LOOKUP_DELAY_S)
        return lookup(self, container_id, port)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(DockerClient, "port", lookup_with_retry)
        yield


def _clear_cached_clients() -> None:
    # all four are @lru_cache'd singletons (core/config.py, core/db/engine.py,
    # core/cache/redis_client.py) -- without clearing them, anything already
    # built against the dev DB/Redis URL keeps being reused, test container
    # or not (testing-basics.md §3.6)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()
    get_redis_client.cache_clear()
    # BOOK-64: Notification's own settings and engine, same reason
    get_notification_settings.cache_clear()
    get_notification_engine.cache_clear()
    get_notification_session_factory.cache_clear()
    # BOOK-65: Payment's own engine, same reason
    get_payment_engine.cache_clear()
    get_payment_session_factory.cache_clear()


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

            # BOOK-64: Notification's own `notifications` database, on the same
            # server -- mirrors docker-compose. Created and migrated by the very
            # script compose's notification-migrate runs, so that's under test too.
            mp.setenv("NOTIFICATION_DATABASE_URL", url.rsplit("/", 1)[0] + "/notifications")
            _clear_cached_clients()
            notification_migrate.main()

            # BOOK-65: Payment's own `payments` database, same way -- created and
            # migrated by the script compose's payment-migrate runs
            mp.setenv("PAYMENT_DATABASE_URL", url.rsplit("/", 1)[0] + "/payments")
            _clear_cached_clients()
            payment_migrate.main()

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


# BOOK-64: a real Kafka broker, for the outbox -> relay -> Kafka -> consumer
# pipeline. KRaft mode (no ZooKeeper), same as docker-compose. Not autouse --
# only the pipeline tests pay for starting it.
@pytest.fixture(scope="session")
def kafka_bootstrap_servers() -> Iterator[str]:
    with KafkaContainer(KAFKA_IMAGE).with_kraft() as kafka:
        servers = kafka.get_bootstrap_server()
        with pytest.MonkeyPatch.context() as mp:
            # read by make_producer() / make_consumer() via get_settings()
            mp.setenv("KAFKA_BOOTSTRAP_SERVERS", servers)
            _clear_cached_clients()
            yield servers
        _clear_cached_clients()


# scope="function": a fresh engine per test, disposed at the end -- keeps the
# engine's lifetime the same as the per-test event loop, so no connection
# outlives the loop it was opened on (testing-basics.md §3.5)
@pytest.fixture
async def db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url)
    yield engine
    await engine.dispose()


# BOOK-64: the test's own connection to Notification's database -- the
# counterpart of db_engine, for reading back what the consumer wrote there
@pytest.fixture
async def notification_db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(notification_database_url())
    yield engine
    await engine.dispose()


# BOOK-65: the test's own connection to Payment's database -- for setting up and
# reading back payments, the way notification_db_engine does for Notification
@pytest.fixture
async def payment_db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(get_settings().payment_database_url)
    yield engine
    await engine.dispose()


async def _truncate_all(engine: AsyncEngine, version_table: str) -> None:
    async with engine.begin() as conn:
        tables = (
            await conn.scalars(
                text(
                    "SELECT quote_ident(tablename) FROM pg_tables"
                    " WHERE schemaname = 'public' AND tablename <> :version_table"
                ),
                {"version_table": version_table},
            )
        ).all()
        if tables:
            await conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))


# autouse: every integration test gets this without asking for it -- one
# forgotten fixture would otherwise leak rows into whichever test runs next.
# TRUNCATE, not rollback-per-test: app code commits on its own on purpose
# (e.g. BOOK-55's commit split), which would end a wrapping transaction
# early (testing-basics.md §3.4). Table list read from the catalog rather
# than hardcoded, so a table added by a future migration is covered too.
# BOOK-64/65: every database -- Booking's, Notification's and Payment's.
@pytest.fixture(autouse=True)
async def _truncate_tables(
    db_engine: AsyncEngine,
    notification_db_engine: AsyncEngine,
    payment_db_engine: AsyncEngine,
) -> AsyncIterator[None]:
    yield
    await _truncate_all(db_engine, "alembic_version")
    await _truncate_all(notification_db_engine, "notification_alembic_version")
    await _truncate_all(payment_db_engine, "payment_alembic_version")


@pytest.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    # expire_on_commit=False -- same as the app's own session factory
    # (core/db/engine.py), so objects stay readable after a commit
    async with AsyncSession(db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def payment_db_session(payment_db_engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(payment_db_engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
async def notification_db_session(
    notification_db_engine: AsyncEngine,
) -> AsyncIterator[AsyncSession]:
    async with AsyncSession(notification_db_engine, expire_on_commit=False) as session:
        yield session


# The app's own DB access (UnitOfWork(), get_uow, get_engine()) pointed at
# the test container -- for tests that call services directly, and the base
# api_client builds on. No dependency_overrides: the app's cached
# settings/engine are re-pointed instead, so no app code is swapped out.
#
# Per test, on both ends: clearing the caches up front makes the app build a
# fresh engine on *this* test's event loop; disposing it afterwards stops it
# outliving that loop (testing-basics.md §3.5).
@pytest.fixture
async def app_db(postgres_url: str) -> AsyncIterator[None]:
    _clear_cached_clients()
    yield
    await get_engine().dispose()
    await get_notification_engine().dispose()
    await get_payment_engine().dispose()
    _clear_cached_clients()


# Real HTTP into the real app, in-process -- no server, no port
# (testing-basics.md §3.6). Adds the Redis side on top of app_db: same
# fresh-per-test client, closed afterwards.
@pytest.fixture
async def api_client(redis_url: str, app_db: None) -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    # Redis's counterpart to _truncate_tables -- otherwise one test's
    # rate-limit counters and refresh tokens leak into the next. Through the
    # app's own client, before closing it: reuses its pooled connection
    # instead of opening a new one just to flush.
    redis = get_redis_client()
    await redis.flushdb()
    await redis.aclose()
    # only the Redis client -- the engine is still app_db's to dispose
    get_redis_client.cache_clear()


# BOOK-66: Payment's booking_client, pointed at the app in-process instead of a
# real host:port -- the real /internal endpoint, service-token auth included,
# with no server running. While both still live in one app, Payment is in effect
# calling itself.
@pytest.fixture
def booking_in_process(app_db: None) -> Iterator[None]:
    from app.payment import booking_client

    def in_process_client() -> AsyncClient:
        return AsyncClient(
            transport=ASGITransport(app=create_app()),
            base_url="http://booking",
            timeout=booking_client._TIMEOUT_SECONDS,
        )

    with patch.object(booking_client, "_client", in_process_client):
        yield
