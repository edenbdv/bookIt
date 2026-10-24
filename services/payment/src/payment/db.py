"""BOOK-65: Payment's own database access -- its own model catalog, engine and
unit of work, on the `payments` database. Same idea as Notification's db.py
(BOOK-64), but a unit of work instead of a bare session: Payment's code already
passes `uow` through its layers and uses `uow.payments` / `uow.session`, so
PaymentUnitOfWork keeps exactly that shape.
"""

from collections.abc import AsyncGenerator
from functools import lru_cache
from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from payment.config import get_payment_settings


class PaymentBase(DeclarativeBase):
    """Payment's own model catalog, not the monolith's Base -- its tables live in
    the `payments` database with their own migrations, and Booking's Alembic
    stops seeing them."""


# same lru_cache singleton shape as core/db/engine.py's get_engine()
@lru_cache
def get_payment_engine() -> AsyncEngine:
    return create_async_engine(get_payment_settings().payment_database_url)


@lru_cache
def get_payment_session_factory() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_payment_engine(), expire_on_commit=False)


class PaymentUnitOfWork:
    """Same contract as core's UnitOfWork -- one session, commit on a clean exit,
    roll back if anything raised -- but only Payment's repository, on Payment's
    database."""

    async def __aenter__(self) -> Self:
        # imported here, not at module level: repository.py imports the models,
        # which import PaymentBase from this module
        from payment.repository import PaymentRepository

        self.session = get_payment_session_factory()()
        self.payments = PaymentRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.session.commit()
            else:
                await self.session.rollback()
        finally:
            await self.session.close()


# FastAPI dependency: one PaymentUnitOfWork per request, like core's get_uow()
async def get_payment_uow() -> AsyncGenerator[PaymentUnitOfWork, None]:
    async with PaymentUnitOfWork() as uow:
        yield uow
