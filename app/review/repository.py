from uuid import UUID

from sqlalchemy import select

from app.core.repository import BaseRepository
from app.reservation.models import Reservation
from app.review.models import Review


class ReviewRepository(BaseRepository[Review]):
    model = Review

    async def list_by_property_id(self, property_id: UUID, limit: int, offset: int) -> list[Review]:
        # Review only has reservation_id, not property_id -- join through
        # Reservation to find "this property's reviews" at all
        stmt = (
            select(Review)
            .join(Reservation, Review.reservation_id == Reservation.id)
            .where(Reservation.property_id == property_id)
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.scalars(stmt)
        return list(result)
