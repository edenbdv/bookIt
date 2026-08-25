from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import selectinload

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
            # guest->property reviews only. Reviews live in both directions
            # in one table (models.py: direction is inferred from whether
            # author_id matches the reservation's guest), and a host's review
            # OF a guest ("Great guest") is not feedback about the listing --
            # it also has no business on a public, unauthenticated endpoint.
            #
            # TODO: host->guest reviews are still written by create_review but
            # now have no read path anywhere in the API. They need their own
            # endpoint (GET /users/{user_id}/reviews) backing a guest profile
            # page, incl. a decision on whether it's public or auth-gated.
            .where(Review.author_id == Reservation.guest_id)
            # eager-load the author: service.py reads author.display_name to
            # flatten it onto ReviewRead, and lazy-loading a relationship in
            # async SQLAlchemy raises MissingGreenlet. selectinload, not
            # joinedload -- one extra IN query for the page's authors beats
            # duplicating Review rows across the existing Reservation join.
            .options(selectinload(Review.author))
            .order_by(Review.created_at.desc(), Review.id.desc())
            .limit(limit)
            .offset(offset)
        )
        result = await self.session.scalars(stmt)
        return list(result)
