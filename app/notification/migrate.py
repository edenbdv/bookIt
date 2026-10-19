"""BOOK-64 step 4c: bring Notification's own database up to date. Run with:

    uv run python -m app.notification.migrate

Two things, both safe to repeat on every start:
1. create the database named in NOTIFICATION_DATABASE_URL if it doesn't exist
   yet -- Postgres only runs init scripts on a brand-new volume, so an
   existing dev volume would never get the `notifications` database otherwise;
2. run Notification's own migrations (app/notification/alembic.ini) to head.
"""

import asyncio
import logging
from pathlib import Path

import asyncpg
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from app.notification.db import notification_database_url

logger = logging.getLogger(__name__)

_ALEMBIC_INI = Path(__file__).with_name("alembic.ini")


async def _create_database_if_missing(url: str) -> None:
    target = make_url(url)
    # connect to the server's default `postgres` database to create the target
    server = target.set(drivername="postgresql", database="postgres")
    conn = await asyncpg.connect(server.render_as_string(hide_password=False))
    try:
        exists = await conn.fetchval(
            "SELECT 1 FROM pg_database WHERE datname = $1", target.database
        )
        if exists:
            logger.info("Database %r already exists", target.database)
            return
        # identifiers can't be bind parameters -- quote it explicitly instead
        await conn.execute(f'CREATE DATABASE "{target.database}"')
        logger.info("Created database %r", target.database)
    finally:
        await conn.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    url = notification_database_url()
    asyncio.run(_create_database_if_missing(url))
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


if __name__ == "__main__":
    main()
