"""BOOK-51: entrypoint for the Kafka notification consumer. Run with:

    uv run python -m notification

Own process, not Celery (worker-decisions.md §12). Thin wrapper -- just
wires notification/consumer.py's loop to notification/service.py's
handlers and starts it. Full pipeline: docs/jobs/booking-events-flow.md.
"""

from notification import events
from notification import service as notification_service
from notification.consumer import (
    EventHandler,
    make_consumer,
    make_dead_letter,
    make_dispatcher,
    make_producer,
    run,
)

# event_type (from the Kafka payload) -> the email to send for it
_HANDLERS: dict[str, EventHandler] = {
    # BOOK-64: added before Booking starts publishing BookingCreated (step
    # 3b) -- the receiver learns a new event type before the sender sends it
    events.BOOKING_CREATED: notification_service.send_reservation_received_email,
    events.BOOKING_CONFIRMED: notification_service.send_reservation_confirmed_email,
    events.BOOKING_CANCELLED: notification_service.send_reservation_cancelled_email,
}


def main() -> None:
    # make_dispatcher wraps _HANDLERS with the dedup check (§13); run() polls
    # forever, setting aside anything that fails for good on booking-events.dlq
    run(make_consumer(), make_dispatcher(_HANDLERS), make_dead_letter(make_producer()))


if __name__ == "__main__":
    main()
