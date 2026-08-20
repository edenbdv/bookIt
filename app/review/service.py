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
    return review


async def list_reviews_for_property(
    uow: UnitOfWork, property_id: uuid.UUID, limit: int, offset: int
) -> list[Review]:
    property_ = await uow.properties.get_active_by_id(property_id)
    if property_ is None:
        raise PropertyNotFoundError
    return await uow.reviews.list_by_property_id(property_id, limit, offset)
