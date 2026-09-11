"""BOOK-51: entrypoint for the Kafka notification consumer. Run with:

    uv run python -m app.notification_consumer

Own process, not Celery (worker-decisions.md §12). Thin wrapper -- just
wires app/notification/consumer.py's loop to app/notification/service.py's
handlers and starts it. Full pipeline: docs/jobs/booking-events-flow.md.
"""

from app.core.outbox.models import OutboxEventType
from app.notification import service as notification_service
from app.notification.consumer import EventHandler, make_consumer, make_dispatcher, run

# event_type (from the Kafka payload) -> the email to send for it
_HANDLERS: dict[str, EventHandler] = {
    OutboxEventType.BOOKING_CONFIRMED.value: notification_service.send_reservation_confirmed_email,
    OutboxEventType.BOOKING_CANCELLED.value: notification_service.send_reservation_cancelled_email,
}


def main() -> None:
    # make_dispatcher wraps _HANDLERS with the dedup check (§13); run() polls forever
    run(make_consumer(), make_dispatcher(_HANDLERS))


if __name__ == "__main__":
    main()
