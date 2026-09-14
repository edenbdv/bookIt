from datetime import datetime
from uuid import UUID

from sqlalchemy import select

from app.core.db.repository import BaseRepository
from app.reservation.models import Reservation, ReservationStatus


class ReservationRepository(BaseRepository[Reservation]):
    model = Reservation

    # BOOK-102: a host's own bookings view -- newest first, so an active
    # PENDING/CONFIRMED stay surfaces above old COMPLETED/CANCELLED ones
    async def list_by_property_id(self, property_id: UUID) -> list[Reservation]:
        result = await self.session.execute(
            select(Reservation)
            .where(Reservation.property_id == property_id)
            .order_by(Reservation.created_at.desc())
        )
        return list(result.scalars())

    # BOOK-102: guards property deletion -- PENDING/CONFIRMED are the two
    # non-terminal statuses (reservation/service.py's _LEGAL_TRANSITIONS);
    # a CANCELLED or COMPLETED one has nothing left to disrupt
    async def has_active_by_property_id(self, property_id: UUID) -> bool:
        stmt = (
            select(Reservation.id)
            .where(
                Reservation.property_id == property_id,
                Reservation.status.in_([ReservationStatus.PENDING, ReservationStatus.CONFIRMED]),
            )
            .limit(1)
        )
        return await self.session.scalar(stmt) is not None

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
