"""BOOK-65: Alembic environment for Payment's own database.

Same as Notification's (BOOK-64), with Payment's names:
- target_metadata is PaymentBase.metadata -- only Payment's tables;
- the URL is payment_database_url, Payment's own setting;
- its own version table, payment_alembic_version, so it can never collide with
  Booking's alembic_version if both run against the same database (tests, mid-switch).
"""

import asyncio
from logging.config import fileConfig

# imported for its side effect: patches autogenerate to detect Postgres ENUM
# changes -- Payment has enums too (same plugin as Booking's alembic/env.py)
import alembic_postgresql_enum  # noqa: F401
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# imported for its side effect: the package registers every Payment table on
# PaymentBase (app/payment/models/__init__.py)
import app.payment.models  # noqa: F401
from alembic import context
from app.core.config import get_settings
from app.payment.db import PaymentBase

VERSION_TABLE = "payment_alembic_version"

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False: same reason as Notification's env.py --
    # migrations run in-process (tests) must not switch off existing loggers
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = PaymentBase.metadata


def _url() -> str:
    return get_settings().payment_database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
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
    configuration["sqlalchemy.url"] = _url()

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
