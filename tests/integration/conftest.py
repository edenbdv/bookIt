"""BOOK-61: integration-test fixtures -- a real Postgres, not FakeUnitOfWork.

Only tests under tests/integration/ ever request these, so the unit tests
elsewhere in tests/ still run without Docker. Concepts behind every choice
here: docs/testing/testing-basics.md (§ refs below).
"""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from app.core.config import get_settings
from app.core.db.engine import get_engine, get_session_factory

REPO_ROOT = Path(__file__).resolve().parents[2]

# same image as docker-compose.yml's `db` service -- tests run against the
# Postgres version the app actually runs on
POSTGRES_IMAGE = "postgres:16-alpine"


def _clear_cached_settings_and_engine() -> None:
    # all three are @lru_cache'd singletons (core/config.py, core/db/engine.py)
    # -- without clearing them, anything already built against the dev DB's
    # URL keeps being reused, test container or not (testing-basics.md §3.6)
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_factory.cache_clear()


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
            _clear_cached_settings_and_engine()

            # real migrations, not Base.metadata.create_all() -- btree_gist
            # only exists in a migration (testing-basics.md §3.3)
            command.upgrade(Config(str(REPO_ROOT / "alembic.ini")), "head")

            yield url

        # env var restored by the context manager; drop anything cached
        # against the now-gone container too
        _clear_cached_settings_and_engine()


# scope="function": a fresh engine per test, disposed at the end -- keeps the
# engine's lifetime the same as the per-test event loop, so no connection
# outlives the loop it was opened on (testing-basics.md §3.5)
@pytest.fixture
async def db_engine(postgres_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_url)
    yield engine
    await engine.dispose()
