"""BOOK-65: Booking's side of the payment-events topic -- what a reservation
does when Payment reports a charge's outcome (docs/microservices/epic-11-flow.md).

Event types are plain strings, not imported from app.payment: they arrive
over Kafka from what becomes another service, so the payload is the
contract, not Payment's Python enum.
"""

import logging
import uuid

from app.core.db.unit_of_work import UnitOfWork
from app.reservation import service
from app.reservation.inbox import ProcessedPaymentEvent
from app.reservation.models import ReservationStatus

logger = logging.getLogger(__name__)


async def handle_payment_event(uow: UnitOfWork, payload: dict) -> None:
    """Booking's reaction to one payment-events message. The Inbox row and
    the confirm/cancel share uow's one transaction -- both or neither."""
    event_id = uuid.UUID(payload["event_id"])
    if await uow.session.get(ProcessedPaymentEvent, event_id) is not None:
        return  # redelivery -- already handled

    reservation = await uow.reservations.get_by_id(uuid.UUID(payload["reservation_id"]))
    # only a PENDING reservation is waiting on a payment outcome -- anything
    # else was already decided (e.g. by the saga's direct call, while both
    # paths still run), so there's nothing left to do
    if reservation is not None and reservation.status == ReservationStatus.PENDING:
        if payload["event_type"] == "payment_succeeded":
            await service.confirm_reservation(uow, reservation.id)
        elif payload["event_type"] == "payment_failed":
            await service.cancel_reservation_system(uow, reservation)
    elif (
        reservation is not None
        and reservation.status == ReservationStatus.CANCELLED
        and payload["event_type"] == "payment_succeeded"
    ):
        # money taken for a reservation that's already gone (e.g. its hold
        # expired while the charge was still PENDING) -- nobody refunds it
        # yet. Same gap as today's saga; the fix is an event Payment refunds
        # on (BOOK-68). Logged so it's at least visible.
        logger.warning(
            "Payment %s succeeded for already-cancelled reservation %s -- needs a refund",
            payload["payment_id"],
            reservation.id,
        )

    uow.session.add(ProcessedPaymentEvent(event_id=event_id))
