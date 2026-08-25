import uuid

from sqlalchemy.exc import IntegrityError

from app.core.unit_of_work import UnitOfWork
from app.reservation.models import ReservationStatus
from app.review.models import Review
from app.review.schemas import ReviewCreate


class ReservationNotFoundError(Exception):
    pass


class ReservationNotCompletedError(Exception):
    pass


class NotAParticipantError(Exception):
    pass


class DuplicateReviewError(Exception):
    pass


class PropertyNotFoundError(Exception):
    pass


async def create_review(
    uow: UnitOfWork,
    reservation_id: uuid.UUID,
    author_id: uuid.UUID,
    data: ReviewCreate,
) -> Review:
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError

    if reservation.status != ReservationStatus.COMPLETED:
        raise ReservationNotCompletedError

    # not get_active_by_id -- delisting shouldn't block reviewing a past
    # stay (else a host could dodge a bad review by delisting first).
    # property_.host_id is safe unchecked: property_id is a DB foreign key,
    # and Property is never hard-deleted.
    property_ = await uow.properties.get_by_id(reservation.property_id)
    is_participant = author_id in (reservation.guest_id, property_.host_id)
    if not is_participant:
        raise NotAParticipantError

    review = Review(reservation_id=reservation_id, author_id=author_id, **data.model_dump())
    uow.reviews.add(review)
    try:
        # flush, not commit: populates created_at, and is also what
        # actually runs the DB's UNIQUE (reservation_id, author_id) check
        # -- a duplicate review only surfaces as an IntegrityError here,
        # not before
        await uow.session.flush()
    except IntegrityError as exc:
        raise DuplicateReviewError from exc
    # same non-persisted attribute as list_reviews_for_property below, so the
    # 201 body carries the name too rather than a null the list endpoint
    # would then fill in. review.author isn't loaded here (no eager-load on
    # a freshly-constructed row), so read it off the User directly.
    author = await uow.users.get_by_id(author_id)
    review.author_name = author.display_name
    return review


async def list_reviews_for_property(
    uow: UnitOfWork, property_id: uuid.UUID, limit: int, offset: int
) -> list[Review]:
    property_ = await uow.properties.get_active_by_id(property_id)
    if property_ is None:
        raise PropertyNotFoundError
    reviews = await uow.reviews.list_by_property_id(property_id, limit, offset)
    for review in reviews:
        # not a mapped column -- attached purely so ReviewRead's
        # from_attributes=True can read it as a flat field (same technique as
        # property/service.py's cover_photo); never persisted. The author is
        # eager-loaded by the repository, so this touches no DB.
        review.author_name = review.author.display_name
    return reviews
