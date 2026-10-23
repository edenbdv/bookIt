import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.db.unit_of_work import UnitOfWork
from app.core.idempotency.models import (
    IDEMPOTENCY_KEY_TTL_HOURS,
    IdempotencyKey,
    IdempotencyKeyStatus,
)
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.outbox import PaymentEventType, PaymentOutboxEvent
from app.payment.provider import PaymentOutcome, charge
from app.payment.provider import refund as provider_refund
from app.payment.schemas import PaymentCreate
from app.reservation.models import ReservationStatus

# real card networks/issuing banks take real time to answer -- this mock
# resolves in zero time otherwise, which reads as fake ("did that actually
# charge anything?") rather than fast. provider.py stays a pure, sync
# decision function; the latency belongs here, at the layer that's
# actually standing in for "calling out to a real external system".
_SIMULATED_PROVIDER_LATENCY_SECONDS = (0.4, 1.2)


async def _simulate_provider_latency() -> None:
    await asyncio.sleep(random.uniform(*_SIMULATED_PROVIDER_LATENCY_SECONDS))


class ReservationNotFoundError(Exception):
    pass


# BOOK-55: webhook references a payment id we don't have
class PaymentNotFoundError(Exception):
    pass


class NotReservationGuestError(Exception):
    pass


# BOOK-53 only accepts a charge against a PENDING reservation -- one already
# confirmed/cancelled/completed has nothing left to pay for or against
class ReservationNotPayableError(Exception):
    pass


# a TIMEOUT leaves the reservation itself still 'pending' (react_to_payment_
# outcome no-ops on a PENDING payment), so nothing else blocks a guest from
# retrying -- without this guard, an impatient retry starts a second,
# independent charge attempt while the first's real outcome is still
# unknown. If the first one *did* actually go through, that's a double
# charge with no automatic refund, not just a UX annoyance.
class PendingChargeExistsError(Exception):
    pass


class PaymentIdempotencyKeyInProgressError(Exception):
    pass


# same key reused for a request that doesn't match the original -- e.g. a
# different reservation_id. Stripe's actual behavior for this case, not
# ours until now: reject explicitly, don't silently hand back the old
# result and don't silently treat it as a new request either.
class PaymentIdempotencyKeyMismatchError(Exception):
    pass


# TIMEOUT deliberately maps to PENDING, not FAILED -- BOOK-52's provider.py:
# a timeout means unknown, not confirmed-failed. Left PENDING for BOOK-55's
# webhook to resolve later, same as a real gateway's async confirmation.
OUTCOME_TO_STATUS: dict[PaymentOutcome, PaymentStatus] = {
    PaymentOutcome.SUCCESS: PaymentStatus.SUCCEEDED,
    PaymentOutcome.FAILURE: PaymentStatus.FAILED,
    PaymentOutcome.TIMEOUT: PaymentStatus.PENDING,
}


# BOOK-65: a resolved charge is news for Booking (confirm or cancel) --
# written in the same transaction as the status change, never on its own.
# Same event_id-in-both-places reasoning as reservation/service.py's
# _emit_outbox_event: the consumer's Inbox dedups on it.
_STATUS_TO_EVENT_TYPE: dict[PaymentStatus, PaymentEventType] = {
    PaymentStatus.SUCCEEDED: PaymentEventType.PAYMENT_SUCCEEDED,
    PaymentStatus.FAILED: PaymentEventType.PAYMENT_FAILED,
}


async def emit_payment_event(uow: UnitOfWork, payment: Payment) -> None:
    # PENDING has no outcome yet; a refund's result doesn't drive the reservation
    if payment.kind != PaymentKind.CHARGE or payment.status == PaymentStatus.PENDING:
        return
    event_type = _STATUS_TO_EVENT_TYPE[payment.status]
    event_id = uuid.uuid4()
    uow.session.add(
        PaymentOutboxEvent(
            id=event_id,
            event_type=event_type,
            reservation_id=payment.reservation_id,
            payload={
                "event_id": str(event_id),
                "event_type": event_type.value,
                "reservation_id": str(payment.reservation_id),
                "occurred_at": datetime.now(UTC).isoformat(),
                "payment_id": str(payment.id),
                "amount_cents": payment.amount_cents,
            },
        )
    )


# not wired to confirm_reservation()/cancel_reservation() -- chaining the
# saga's outcome into the reservation is BOOK-54's job, not this one
async def create_payment(
    uow: UnitOfWork,
    reservation_id: uuid.UUID,
    current_user_id: uuid.UUID,
    *,
    force: PaymentOutcome | None = None,
) -> Payment:
    # only the reservation's own guest can pay for it, and only while
    # PENDING -- nothing left to pay for/against otherwise
    reservation = await uow.reservations.get_by_id(reservation_id)
    if reservation is None:
        raise ReservationNotFoundError
    if reservation.guest_id != current_user_id:
        raise NotReservationGuestError
    if reservation.status != ReservationStatus.PENDING:
        raise ReservationNotPayableError

    # blocks a second charge attempt while an earlier TIMEOUT for this
    # reservation is still unresolved -- see PendingChargeExistsError
    existing_pending = await uow.payments.get_pending_charge_by_reservation_id(reservation.id)
    if existing_pending is not None:
        raise PendingChargeExistsError

    # record the attempt before calling the provider, not after
    payment = Payment(
        reservation_id=reservation.id,
        kind=PaymentKind.CHARGE,
        amount_cents=reservation.total_cents,
        status=PaymentStatus.PENDING,
    )
    uow.payments.add(payment)
    await uow.session.flush()
    # BOOK-55: commit before the external call, so this PENDING row (and
    # payment.id, the correlation id) survives a crash during charge()
    await uow.session.commit()

    # skipped when force= is set -- tests/scripts pin a deterministic
    # outcome specifically to run fast and not wait on this
    if force is None:
        await _simulate_provider_latency()

    # BOOK-52's mock -- outcome maps to the payment's real status below
    outcome = charge(payment.amount_cents, correlation_id=payment.id, force=force)
    payment.status = OUTCOME_TO_STATUS[outcome]
    await emit_payment_event(uow, payment)
    return payment


async def create_payment_idempotent(
    uow: UnitOfWork,
    data: PaymentCreate,
    current_user_id: uuid.UUID,
    idempotency_key: str,
    *,
    force: PaymentOutcome | None = None,
) -> Payment:
    # try to claim this key -- the PK constraint is what actually closes
    # the concurrent-duplicate-key race
    key_row = IdempotencyKey(
        key=idempotency_key,
        expires_at=datetime.now(UTC) + timedelta(hours=IDEMPOTENCY_KEY_TTL_HOURS),
    )
    uow.session.add(key_row)
    try:
        await uow.session.flush()
    except IntegrityError:
        # key already exists -- this is a retry, not a first attempt
        await uow.session.rollback()
        existing = await uow.session.get(IdempotencyKey, idempotency_key)
        if existing.status == IdempotencyKeyStatus.COMPLETED:
            existing_payment = await uow.payments.get_by_id(existing.payment_id)
            existing_reservation = await uow.reservations.get_by_id(existing_payment.reservation_id)
            # keys aren't scoped per-guest -- without this check, a different
            # guest reusing/guessing this key would get back someone else's
            # payment as their own request's result
            if existing_reservation.guest_id != current_user_id:
                raise PaymentIdempotencyKeyInProgressError
            # same key, different reservation_id than the original request
            if existing_payment.reservation_id != data.reservation_id:
                raise PaymentIdempotencyKeyMismatchError
            return existing_payment
        # still PROCESSING -- a concurrent request for the same key is mid-flight
        raise PaymentIdempotencyKeyInProgressError

    # first time seeing this key -- actually do the work
    payment = await create_payment(uow, data.reservation_id, current_user_id, force=force)
    # COMPLETED means "this request attempt is done", not "payment succeeded" --
    # true on a TIMEOUT too, so a retry returns the same PENDING payment
    # instead of charging again (BOOK-55's webhook resolves it later)
    key_row.status = IdempotencyKeyStatus.COMPLETED
    key_row.payment_id = payment.id
    return payment


async def create_refund(uow: UnitOfWork, reservation_id: uuid.UUID, amount_cents: int) -> Payment:
    """Mirror of create_payment(): commit the refund PENDING, then call the
    provider -- a crash mid-call still leaves a row to resolve (BOOK-55).
    Takes a reservation_id, not a Reservation: Payment won't have Booking's
    objects once split out (BOOK-65)."""
    refund_payment = Payment(
        reservation_id=reservation_id,
        kind=PaymentKind.REFUND,
        amount_cents=amount_cents,
        status=PaymentStatus.PENDING,
    )
    uow.payments.add(refund_payment)
    await uow.session.flush()
    await uow.session.commit()

    await _simulate_provider_latency()
    outcome = provider_refund(amount_cents, correlation_id=refund_payment.id)
    refund_payment.status = OUTCOME_TO_STATUS[outcome]
    return refund_payment
