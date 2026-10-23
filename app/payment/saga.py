"""Payment's entrypoints: charge, resolve (webhook), reconcile.

BOOK-65: no longer reacts to the outcome itself. The saga is now choreography
over Kafka -- a resolved charge's payment_succeeded / payment_failed goes out
through Payment's outbox, and Booking confirms/cancels from payment-events
(reservation/payment_events.py). A payment Booking can't use comes back as
booking_payment_rejected and is refunded (payment/booking_events.py).
"""

import uuid
from datetime import datetime

from app.core.db.unit_of_work import UnitOfWork
from app.payment import service
from app.payment.models import Payment, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.provider import check_status as provider_check_status
from app.payment.schemas import PaymentCreate


async def create_payment_and_react(
    uow: UnitOfWork,
    data: PaymentCreate,
    current_user_id: uuid.UUID,
    idempotency_key: str,
    *,
    force: PaymentOutcome | None = None,
) -> Payment:
    """Charge, then commit the status with its outbox event. Booking reacts
    from payment-events, a few seconds later."""
    payment = await service.create_payment_idempotent(
        uow, data, current_user_id, idempotency_key, force=force
    )
    await uow.session.commit()
    return payment


async def resolve_payment_and_react(
    uow: UnitOfWork, payment_id: uuid.UUID, outcome: PaymentOutcome
) -> Payment:
    """BOOK-55: webhook entrypoint -- resolve a PENDING payment and commit its
    event. No-ops on an already-resolved payment (redelivery-safe)."""
    payment = await uow.payments.get_by_id(payment_id)
    if payment is None:
        raise service.PaymentNotFoundError
    if payment.status != PaymentStatus.PENDING:
        return payment  # already resolved -- redelivery, no-op

    payment.status = service.OUTCOME_TO_STATUS[outcome]
    await service.emit_payment_event(uow, payment)
    await uow.session.commit()
    return payment


# BOOK-101: the pull counterpart to the webhook's push -- reuses
# resolve_payment_and_react so both share the same redelivery-safe guarantee
async def reconcile_stale_payments(uow: UnitOfWork, older_than: datetime) -> int:
    stale = await uow.payments.list_stale_pending(older_than)  # find stuck rows
    for payment in stale:
        outcome = provider_check_status(correlation_id=payment.id)  # ask the provider
        await resolve_payment_and_react(uow, payment.id, outcome)  # same resolver the webhook uses
    return len(stale)
