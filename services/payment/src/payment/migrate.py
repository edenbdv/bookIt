"""BOOK-65: bring Payment's own database up to date. Run with:

    uv run python -m payment.migrate

Same as Notification's migrate.py (BOOK-64), safe to repeat on every start:
1. create the database named in PAYMENT_DATABASE_URL if it doesn't exist yet --
   Postgres only runs init scripts on a brand-new volume;
2. run Payment's own migrations (services/payment/src/payment/alembic.ini) to head.
"""

import asyncio
import logging
from pathlib import Path

import asyncpg
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command
from payment.config import get_payment_settings

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
    asyncio.run(_create_database_if_missing(get_payment_settings().payment_database_url))
    command.upgrade(Config(str(_ALEMBIC_INI)), "head")


if __name__ == "__main__":
    main()
