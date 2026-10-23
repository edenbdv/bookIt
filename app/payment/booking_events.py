"""BOOK-65: Payment's side of the booking-events topic -- what Payment does
when Booking reports something about a reservation (docs/microservices/epic-11-flow.md).

Event types are plain strings, not imported from Booking's OutboxEventType:
they arrive over Kafka from another service, so the payload is the contract.
"""

import uuid

from app.core.db.unit_of_work import UnitOfWork
from app.payment import service
from app.payment.inbox import ProcessedBookingEvent
from app.payment.models import PaymentKind, PaymentStatus


async def handle_booking_event(uow: UnitOfWork, payload: dict) -> None:
    """Payment's reaction to one booking-events message."""
    # every other type (booking_created/confirmed/cancelled) has no effect
    # here -- skipped without an Inbox row, which only guards real effects
    if payload["event_type"] != "booking_payment_rejected":
        return

    event_id = uuid.UUID(payload["event_id"])
    if await uow.session.get(ProcessedBookingEvent, event_id) is not None:
        return  # redelivery -- already handled
    # added first, on purpose: create_refund() commits mid-way (PENDING row
    # before the provider call), and this Inbox row must ride in that same
    # commit -- a crash during the provider call then can't lead to a second
    # refund on redelivery. Reconcile finishes the PENDING refund.
    uow.session.add(ProcessedBookingEvent(event_id=event_id))

    charge = await uow.payments.get_by_id(uuid.UUID(payload["payment_id"]))
    # only a charge that really succeeded has money to give back. The amount
    # comes from Payment's own row, not the event -- the source of truth.
    if (
        charge is not None
        and charge.kind == PaymentKind.CHARGE
        and charge.status == PaymentStatus.SUCCEEDED
    ):
        await service.create_refund(uow, charge.reservation_id, charge.amount_cents)
