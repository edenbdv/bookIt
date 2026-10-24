"""BOOK-65: Payment's side of the booking-events topic -- what Payment does
when Booking reports something about a reservation (docs/microservices/epic-11-flow.md).

Event types are plain strings, not imported from Booking's OutboxEventType:
they arrive over Kafka from another service, so the payload is the contract.
"""

import uuid

from app.core.db.unit_of_work import UnitOfWork
from app.payment import service
from app.payment.models import PaymentKind, PaymentStatus, ProcessedBookingEvent


def _has_effect(payload: dict) -> bool:
    if payload["event_type"] == "booking_payment_rejected":
        return True
    # refund_cents is Booking's policy decision (0 = unpaid, or nothing back);
    # .get(): booking_cancelled events from before BOOK-65 don't carry it
    if payload["event_type"] == "booking_cancelled":
        return payload.get("refund_cents", 0) > 0
    return False


async def handle_booking_event(uow: UnitOfWork, payload: dict) -> None:
    """Payment's reaction to one booking-events message."""
    # anything with no effect here (booking_created/confirmed, a cancel with
    # nothing to refund) is skipped without an Inbox row -- it only guards effects
    if not _has_effect(payload):
        return

    event_id = uuid.UUID(payload["event_id"])
    if await uow.session.get(ProcessedBookingEvent, event_id) is not None:
        return  # redelivery -- already handled
    # added first, on purpose: create_refund() commits mid-way (PENDING row
    # before the provider call), and this Inbox row must ride in that same
    # commit -- a crash during the provider call then can't lead to a second
    # refund on redelivery. Reconcile finishes the PENDING refund.
    uow.session.add(ProcessedBookingEvent(event_id=event_id))

    if payload["event_type"] == "booking_cancelled":
        charge = await uow.payments.get_succeeded_charge_by_reservation_id(
            uuid.UUID(payload["reservation_id"])
        )
        if charge is not None:
            # the amount is Booking's (the policy lives there); the cap is ours
            amount = min(payload["refund_cents"], charge.amount_cents)
            await service.create_refund(uow, charge.reservation_id, amount)
        return

    # booking_payment_rejected
    charge = await uow.payments.get_by_id(uuid.UUID(payload["payment_id"]))
    # only a charge that really succeeded has money to give back. The amount
    # comes from Payment's own row, not the event -- the source of truth.
    if (
        charge is not None
        and charge.kind == PaymentKind.CHARGE
        and charge.status == PaymentStatus.SUCCEEDED
    ):
        await service.create_refund(uow, charge.reservation_id, charge.amount_cents)
