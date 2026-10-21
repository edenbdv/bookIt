from collections.abc import AsyncGenerator
from types import TracebackType
from typing import Self

from app.auth.repository import UserRepository
from app.core.db.engine import get_session_factory
from app.payment.repository import PaymentRepository
from app.property.repository import PropertyPhotoRepository, PropertyRepository
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
        self.property_photos = PropertyPhotoRepository(self.session)
        self.reservations = ReservationRepository(self.session)
        self.payments = PaymentRepository(self.session)
        self.reviews = ReviewRepository(self.session)
        return self

    # BOOK-64 removed on_commit(): its only caller was the Celery .delay() for
    # the "reservation received" email, now a BookingCreated outbox event. For
    # a side effect that must not be lost, write an outbox row in the same
    # transaction -- an in-memory after-commit hook loses it on a crash
    # (worker-decisions.md §9).

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


# FastAPI dependency: one UnitOfWork per request -- commits when the route
# returns normally, rolls back if the route raised. Same "one request, one
# session" shape as get_db(), just handing routes a UnitOfWork instead of a
# bare AsyncSession.
async def get_uow() -> AsyncGenerator[UnitOfWork, None]:
    async with UnitOfWork() as uow:
        yield uow
