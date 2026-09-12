"""BOOK-54: react to a resolved payment outcome -- confirm the reservation
on success, compensate (cancel it) on failure. Orchestration, not
choreography (saga-basics.md §4) -- one function explicitly sequencing
both steps, same reasoning worker-decisions.md §9 already used for BOOK-45.

Idempotent-safe against BOOK-53's own idempotent endpoint and BOOK-55's
future webhook redeliveries: checks the reservation's current status
before acting, so a repeat call is a no-op, not an IllegalTransitionError.
"""

import uuid

from app.core.db.unit_of_work import UnitOfWork
from app.payment import service
from app.payment.models import Payment, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.schemas import PaymentCreate
from app.reservation import service as reservation_service
from app.reservation.models import ReservationStatus


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
        await reservation_service.confirm_reservation(uow, reservation.id)
    elif payment.status == PaymentStatus.FAILED:
        # compensating action -- not cancel_reservation(), which requires a
        # guest current_user_id this system-triggered path doesn't have
        reservation_service.cancel_reservation_system(uow, reservation)


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
