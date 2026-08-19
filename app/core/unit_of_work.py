from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Self

from app.auth.repository import UserRepository
from app.core.db import get_session_factory
from app.payment.repository import PaymentRepository
from app.property.repository import PropertyRepository
from app.reservation.repository import ReservationRepository
from app.review.repository import ReviewRepository


class UnitOfWork:
    """One transaction, one Session -- commits on clean exit, rolls back if
    an exception propagates. Every repository attribute shares this same
    session, so writes through any of them land in the same transaction.
    """

    async def __aenter__(self) -> Self:
        self.session = get_session_factory()()
        self.users = UserRepository(self.session)
        self.properties = PropertyRepository(self.session)
        self.reservations = ReservationRepository(self.session)
        self.payments = PaymentRepository(self.session)
        self.reviews = ReviewRepository(self.session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if exc_type is None:
            await self.session.commit()
        else:
            await self.session.rollback()
        await self.session.close()


# FastAPI dependency: one UnitOfWork per request -- commits when the route
# returns normally, rolls back if the route raised. Same "one request, one
# session" shape as get_db(), just handing routes a UnitOfWork instead of a
# bare AsyncSession.
async def get_uow() -> AsyncGenerator[UnitOfWork, None]:
    async with UnitOfWork() as uow:
        yield uow
