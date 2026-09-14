from fastapi import APIRouter, Depends, Header, HTTPException, Request, status

from app.auth.dependencies import enforce_user_rate_limit, get_current_user
from app.auth.models import User
from app.core.db.unit_of_work import UnitOfWork, get_uow
from app.payment import saga, service
from app.payment.models import Payment
from app.payment.schemas import PaymentCreate, PaymentRead, PaymentWebhookPayload
from app.payment.webhook_auth import InvalidWebhookSignatureError, verify_webhook_signature

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
        return await saga.create_payment_and_react(uow, data, current_user.id, idempotency_key)
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
    except service.PendingChargeExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A previous payment attempt for this reservation is still processing",
        ) from exc


# BOOK-55: no get_current_user/rate limit -- the caller is the provider,
# not a logged-in guest. The signature check below is what authenticates it.
@router.post("/webhook", status_code=status.HTTP_200_OK)
async def payment_webhook(
    request: Request,
    x_webhook_signature: str = Header(..., alias="X-Webhook-Signature"),
    uow: UnitOfWork = Depends(get_uow),
) -> dict[str, str]:
    body = (
        await request.body()
    )  # raw bytes -- signature is verified over these, not a reparsed model
    try:
        verify_webhook_signature(body, x_webhook_signature)
    except InvalidWebhookSignatureError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid signature"
        ) from exc

    payload = PaymentWebhookPayload.model_validate_json(body)  # only parsed once verified
    try:
        await saga.resolve_payment_and_react(uow, payload.payment_id, payload.outcome)
    except service.PaymentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
        ) from exc

    return {"status": "ok"}
