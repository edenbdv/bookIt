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
