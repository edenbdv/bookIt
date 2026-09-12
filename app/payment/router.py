from fastapi import APIRouter, Depends, Header, HTTPException, status

from app.auth.dependencies import enforce_user_rate_limit, get_current_user
from app.auth.models import User
from app.core.db.unit_of_work import UnitOfWork, get_uow
from app.payment import saga, service
from app.payment.models import Payment
from app.payment.schemas import PaymentCreate, PaymentRead

router = APIRouter(prefix="/payments", tags=["payments"])


@router.post("", response_model=PaymentRead, status_code=status.HTTP_201_CREATED)
async def create_payment(
    data: PaymentCreate,
    idempotency_key: str = Header(..., alias="Idempotency-Key"),
    current_user: User = Depends(get_current_user),
    _rl: None = Depends(enforce_user_rate_limit),
    uow: UnitOfWork = Depends(get_uow),
) -> Payment:
    try:
        payment = await service.create_payment_idempotent(
            uow, data, current_user.id, idempotency_key
        )
        await saga.react_to_payment_outcome(uow, payment)
        return payment
    except service.ReservationNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Reservation not found"
        ) from exc
    except service.NotReservationGuestError as exc:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only this reservation's guest may pay for it",
        ) from exc
    except service.ReservationNotPayableError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This reservation is not awaiting payment",
        ) from exc
    except service.PaymentIdempotencyKeyInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A request with this Idempotency-Key is already being processed",
        ) from exc
    except service.PaymentIdempotencyKeyMismatchError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This Idempotency-Key was already used with different request parameters",
        ) from exc
