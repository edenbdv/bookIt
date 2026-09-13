"""BOOK-54: react to a resolved payment outcome -- confirm the reservation
on success, compensate (cancel it) on failure. Orchestration, not
choreography (saga-basics.md §4) -- one function explicitly sequencing
both steps, same reasoning worker-decisions.md §9 already used for BOOK-45.

Idempotent-safe against BOOK-53's own idempotent endpoint and BOOK-55's
webhook redeliveries: checks the reservation's current status before
acting, so a repeat call is a no-op, not an IllegalTransitionError.

BOOK-55: `resolve_payment_and_react()` below is the webhook's entrypoint
into this saga -- resolves a PENDING payment, then reacts to it.
"""

import logging
import uuid

from app.core.db.unit_of_work import UnitOfWork
from app.payment import service
from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.provider import refund as provider_refund
from app.payment.schemas import PaymentCreate
from app.reservation import service as reservation_service
from app.reservation.models import Reservation, ReservationStatus

logger = logging.getLogger(__name__)


async def react_to_payment_outcome(uow: UnitOfWork, payment: Payment) -> None:
    # PENDING covers both "never resolved" and BOOK-52's TIMEOUT (mapped to
    # PENDING in payment/service.py) -- nothing to react to yet either way
    if payment.status == PaymentStatus.PENDING:
        return

    reservation = await uow.reservations.get_by_id(payment.reservation_id)
    # a retry returns the same resolved payment (create_payment_idempotent
    # only guards creation, not this function running twice) -- without
    # this check, confirm_reservation() would raise on an already-CONFIRMED
    # reservation instead of no-op-ing
    if reservation.status != ReservationStatus.PENDING:
        return

    if payment.status == PaymentStatus.SUCCEEDED:
        try:
            await reservation_service.confirm_reservation(uow, reservation.id)
        except Exception:
            # broad on purpose -- whatever the cause, the response is
            # identical (compensate), and re-raising below means nothing
            # is hidden, just handled before it propagates
            logger.exception(
                "confirm_reservation failed after a successful charge (payment %s) -- compensating",
                payment.id,
            )
            await _refund_and_release(uow, payment, reservation)
            raise
    elif payment.status == PaymentStatus.FAILED:
        # compensating action -- not cancel_reservation(), which requires a
        # guest current_user_id this system-triggered path doesn't have
        reservation_service.cancel_reservation_system(uow, reservation)


# BOOK-54: charged but couldn't confirm -- refund and release the hold.
async def _refund_and_release(uow: UnitOfWork, payment: Payment, reservation: Reservation) -> None:
    # captured before rollback() -- it expires every attribute on every
    # persistent object in the session, not just the failed transaction's
    reservation_id = reservation.id
    amount_cents = payment.amount_cents

    await uow.session.rollback()  # discard confirm_reservation()'s half-mutated state
    reservation = await uow.reservations.get_by_id(reservation_id)

    # BOOK-55: mint + commit PENDING before calling the provider, same as
    # service.create_payment() -- refund_payment.id is the correlation id
    refund_payment = Payment(
        reservation_id=reservation.id,
        kind=PaymentKind.REFUND,
        amount_cents=amount_cents,
        status=PaymentStatus.PENDING,
    )
    uow.payments.add(refund_payment)
    await uow.session.flush()
    await uow.session.commit()

    outcome = provider_refund(amount_cents, correlation_id=refund_payment.id)
    refund_payment.status = service.OUTCOME_TO_STATUS[outcome]
    # unconditional -- the hold releases either way (unchanged from BOOK-54)
    reservation_service.cancel_reservation_system(uow, reservation)
    await uow.session.commit()


async def create_payment_and_react(
    uow: UnitOfWork,
    data: PaymentCreate,
    current_user_id: uuid.UUID,
    idempotency_key: str,
    *,
    force: PaymentOutcome | None = None,
) -> Payment:
    """The saga's real entrypoint -- charge, commit that fact on its own,
    then react. Two commits, not one: charging the provider is external and
    irreversible (outbox-basics.md §2's same reasoning). If the charge and
    the reaction shared one transaction and the reaction step then failed,
    the whole thing would roll back -- including the record that the charge
    happened -- while the real charge still stands. Committing right after
    the charge resolves means a later failure can no longer erase that.
    """
    payment = await service.create_payment_idempotent(
        uow, data, current_user_id, idempotency_key, force=force
    )
    await uow.session.commit()
    await react_to_payment_outcome(uow, payment)
    return payment


async def resolve_payment_and_react(
    uow: UnitOfWork, payment_id: uuid.UUID, outcome: PaymentOutcome
) -> Payment:
    """BOOK-55: webhook entrypoint -- resolve a PENDING payment, then react.
    No-ops on an already-resolved payment (redelivery-safe)."""
    payment = await uow.payments.get_by_id(payment_id)
    if payment is None:
        raise service.PaymentNotFoundError
    if payment.status != PaymentStatus.PENDING:
        return payment  # already resolved -- redelivery, no-op

    payment.status = service.OUTCOME_TO_STATUS[outcome]
    await uow.session.commit()  # resolve first, react second -- own commits

    if payment.kind == PaymentKind.CHARGE:
        # only a CHARGE drives the reservation; a REFUND's hold already released
        await react_to_payment_outcome(uow, payment)
    return payment
