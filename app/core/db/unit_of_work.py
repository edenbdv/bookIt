import logging
from collections.abc import AsyncGenerator, Callable
from types import TracebackType
from typing import Self

from app.auth.repository import UserRepository
from app.core.db.engine import get_session_factory
from app.payment.repository import PaymentRepository
from app.property.repository import PropertyPhotoRepository, PropertyRepository
from app.reservation.repository import ReservationRepository
from app.review.repository import ReviewRepository

logger = logging.getLogger(__name__)


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
        self._on_commit: list[Callable[[], None]] = []
        return self

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Run `callback` only if this transaction commits (worker-decisions.md §9).

        In-memory only, not durable -- a crash right after commit() still
        loses it silently. BOOK-50's outbox pattern is the durable version.
        """
        self._on_commit.append(callback)

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc_type is None:
                await self.session.commit()
                # each isolated: a broker outage in one callback (e.g.
                # send_reservation_received_email.delay() failing) must not
                # turn an already-committed reservation into a 500 for the
                # guest, and must not stop unrelated callbacks from running
                for callback in self._on_commit:
                    try:
                        callback()
                    except Exception:
                        logger.exception("on_commit callback failed: %r", callback)
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
