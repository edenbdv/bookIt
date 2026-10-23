"""BOOK-76: the guest-cancel entrypoint. Deliberately its own module, not
inside saga.py -- this isn't a saga step (payment-decisions.md §9: nothing
branches on the refund's outcome, no compensation), just a caller of
service.create_refund(), the external-call durability machinery saga.py's
actual saga also happens to use. Keeping it out of saga.py makes "is this a
saga decision" visible from the file layout, not just a comment.
"""

import uuid
from datetime import UTC, datetime

from app.core.db.unit_of_work import UnitOfWork
from app.payment import service
from app.reservation import service as reservation_service
from app.reservation.cancellation_policy import calculate_refund_cents
from app.reservation.models import Reservation


async def cancel_reservation_and_refund(
    uow: UnitOfWork, reservation_id: uuid.UUID, current_user_id: uuid.UUID
) -> Reservation:
    """Cancels via reservation_service (ownership check + transition,
    unchanged from BOOK-38), then -- if there's a successful charge --
    computes a policy-based refund and processes it, even when the amount
    is 0 (still a real refund attempt, just for nothing, so there's a
    record the policy was evaluated).
    """
    reservation = await reservation_service.cancel_reservation(uow, reservation_id, current_user_id)

    charge_payment = await uow.payments.get_succeeded_charge_by_reservation_id(reservation.id)
    refund_amount_cents = 0
    if charge_payment is not None:
        property_ = await uow.properties.get_by_id(reservation.property_id)
        refund_amount_cents = calculate_refund_cents(
            property_.cancellation_policy,
            reservation.check_in,
            datetime.now(UTC),
            charge_payment.amount_cents,
        )
        await service.create_refund(uow, reservation.id, refund_amount_cents)

    # non-persisted -- attached purely for ReservationCancelRead's
    # from_attributes read (same technique as property/service.py's
    # cover_photo/rating_avg)
    reservation.refund_amount_cents = refund_amount_cents
    return reservation


# BOOK-80: host-cancel counterpart -- always a full refund, never
# calculate_refund_cents' policy-based partial (property-decisions.md)
async def host_cancel_reservation_and_refund(
    uow: UnitOfWork, reservation_id: uuid.UUID, current_user_id: uuid.UUID
) -> Reservation:
    reservation = await reservation_service.host_cancel_reservation(
        uow, reservation_id, current_user_id
    )

    charge_payment = await uow.payments.get_succeeded_charge_by_reservation_id(reservation.id)
    refund_amount_cents = 0
    if charge_payment is not None:
        refund_amount_cents = charge_payment.amount_cents
        await service.create_refund(uow, reservation.id, refund_amount_cents)

    reservation.refund_amount_cents = refund_amount_cents
    return reservation
