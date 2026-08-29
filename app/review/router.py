import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.auth.dependencies import enforce_user_rate_limit, get_current_user
from app.auth.models import User
from app.core.unit_of_work import UnitOfWork, get_uow
from app.review import service
from app.review.models import Review
from app.review.schemas import ReviewCreate, ReviewRead

router = APIRouter(tags=["reviews"])


@router.post(
    "/reservations/{reservation_id}/reviews",
    response_model=ReviewRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(
    reservation_id: uuid.UUID,
    data: ReviewCreate,
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(enforce_user_rate_limit),
    uow: UnitOfWork = Depends(get_uow),
) -> Review:
    try:
        return await service.create_review(uow, reservation_id, current_user.id, data)
    except service.ReservationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        ) from exc
    except service.ReservationNotCompletedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Reservation is not completed"
        ) from exc
    except service.NotAParticipantError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only participants in this reservation may review it",
        ) from exc
    except service.DuplicateReviewError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You have already reviewed this reservation",
        ) from exc


@router.get("/properties/{property_id}/reviews", response_model=list[ReviewRead])
# lives here, not property/router.py: returns list[Review]/ReviewRead, not
# Property -- URL nesting doesn't decide module ownership, return type does
async def list_property_reviews(
    property_id: uuid.UUID,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    uow: UnitOfWork = Depends(get_uow),
) -> list[Review]:
    try:
        return await service.list_reviews_for_property(uow, property_id, limit, offset)
    except service.PropertyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Property not found"
        ) from exc
