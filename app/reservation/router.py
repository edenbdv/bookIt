import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.core.unit_of_work import UnitOfWork, get_uow
from app.reservation import service
from app.reservation.models import Reservation
from app.reservation.schemas import ReservationCreate, ReservationRead

router = APIRouter(prefix="/reservations", tags=["reservations"])


@router.post("", response_model=ReservationRead, status_code=status.HTTP_201_CREATED)
async def create_reservation(
    data: ReservationCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> Reservation:
    try:
        return await service.create_reservation_idempotent(
            uow, data, current_user.id, idempotency_key
        )
    except service.PropertyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Property not found"
        ) from exc
    except service.SelfBookingError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="You can't book your own property"
        ) from exc
    except service.GuestCountExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="guest_count exceeds this property's max_guests",
        ) from exc
    except service.ReservationConflictError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="These dates are no longer available for this property",
        ) from exc
    except service.IdempotencyKeyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A request with this Idempotency-Key is already being processed",
        ) from exc


# manual stand-in for Epic 8's (BOOK-10) payment webhook -- see
# service.confirm_reservation's docstring-comment and
# docs/domain-layer/state-machine-basics.md
@router.post("/{reservation_id}/confirm", response_model=ReservationRead)
async def confirm_reservation(
    reservation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> Reservation:
    try:
        return await service.confirm_reservation(uow, reservation_id)
    except service.ReservationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        ) from exc
    except service.IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


# guest-only (BOOK-38) -- host-initiated cancellation is BOOK-80, not this
@router.post("/{reservation_id}/cancel", response_model=ReservationRead)
async def cancel_reservation(
    reservation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    uow: UnitOfWork = Depends(get_uow),
) -> Reservation:
    try:
        return await service.cancel_reservation(uow, reservation_id, current_user.id)
    except service.ReservationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        ) from exc
    except service.NotReservationGuestError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this reservation's guest may cancel it",
        ) from exc
    except service.IllegalTransitionError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
