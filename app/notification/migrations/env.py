"""BOOK-64 step 4c: Alembic environment for Notification's own database.

Same async shape as the monolith's alembic/env.py, with three differences:
- target_metadata is NotificationBase.metadata -- only Notification's
  tables, never Booking's;
- the URL is notification_database_url(), Notification's own setting;
- its own version table, so it can never collide with Booking's
  alembic_version if both ever run against the same database (tests, or
  mid-switch).
"""

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context
from app.notification.db import notification_database_url
from app.notification.models import NotificationBase

VERSION_TABLE = "notification_alembic_version"

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: the default (True) silently switches off
    # every logger already created -- e.g. the consumer's -- whenever
    # migrations run in-process (as in tests)
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = NotificationBase.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=notification_database_url(),
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = notification_database_url()

    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
