from datetime import datetime

from sqlalchemy import select

from app.core.db.repository import BaseRepository
from app.reservation.models import Reservation, ReservationStatus


class ReservationRepository(BaseRepository[Reservation]):
    model = Reservation

    # BOOK-46: locked (FOR UPDATE) rather than a plain SELECT -- the caller
    # still has to run transition_to() on each row (a Python-level check, not
    # expressible as a WHERE clause), so the lock is what keeps that
    # read-then-decide-then-write sequence safe under concurrency, not the
    # WHERE clause alone. See docs/db/concurrency-basics.md section 6.
    async def list_expired_pending(self, now: datetime) -> list[Reservation]:
        result = await self.session.execute(
            select(Reservation)
            .where(Reservation.status == ReservationStatus.PENDING, Reservation.expires_at < now)
            .with_for_update()
        )
        return list(result.scalars())
