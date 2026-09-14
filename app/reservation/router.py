import uuid

from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.auth.dependencies import enforce_user_rate_limit, get_current_user
from app.auth.models import User
from app.core.db.unit_of_work import UnitOfWork, get_uow
from app.payment import cancellation as payment_cancellation
from app.reservation import service
from app.reservation.models import Reservation
from app.reservation.schemas import ReservationCancelRead, ReservationCreate, ReservationRead

# no prefix, same reasoning as review/router.py: this module owns
# list[Reservation]/ReservationRead endpoints regardless of URL nesting, and
# list_reservations_for_property below lives under /properties/, not /reservations/
router = APIRouter(tags=["reservations"])


@router.post("/reservations", response_model=ReservationRead, status_code=status.HTTP_201_CREATED)
async def create_reservation(
    data: ReservationCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(enforce_user_rate_limit),
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
    except service.IdempotencyKeyMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This Idempotency-Key was already used with different request parameters",
        ) from exc


# manual stand-in for Epic 8's (BOOK-10) payment webhook -- see
# service.confirm_reservation's docstring-comment and
# docs/domain-layer/state-machine-basics.md
@router.post("/reservations/{reservation_id}/confirm", response_model=ReservationRead)
async def confirm_reservation(
    reservation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(enforce_user_rate_limit),
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


# BOOK-102: host-only, for the "my properties" page's bookings list
@router.get("/properties/{property_id}/reservations", response_model=list[ReservationRead])
async def list_reservations_for_property(
    property_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(enforce_user_rate_limit),
    uow: UnitOfWork = Depends(get_uow),
) -> list[Reservation]:
    try:
        return await service.list_reservations_for_property(uow, property_id, current_user.id)
    except service.PropertyNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Property not found"
        ) from exc
    except service.NotPropertyHostError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this property's host may view its reservations",
        ) from exc


# guest-only (BOOK-38) -- host-initiated cancellation is BOOK-80, not this.
# BOOK-76: routes through payment.cancellation, not service.cancel_reservation
# directly -- refund calculation needs the payment/property lookups the
# reservation module deliberately doesn't know about (cancellation.py's docstring).
@router.post("/reservations/{reservation_id}/cancel", response_model=ReservationCancelRead)
async def cancel_reservation(
    reservation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(enforce_user_rate_limit),
    uow: UnitOfWork = Depends(get_uow),
) -> Reservation:
    try:
        return await payment_cancellation.cancel_reservation_and_refund(
            uow, reservation_id, current_user.id
        )
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
