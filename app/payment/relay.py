"""BOOK-65: Payment's own relay -- publishes `payment_outbox_events` rows to
the `payment-events` topic. Same polling relay as core/outbox/relay.py
(BOOK-50, worker-decisions.md §11), kept as its own copy on purpose: it
moves out with Payment when Payment becomes its own service, while
Booking's relay stays behind with Booking's outbox.
"""

import json
import logging
from datetime import UTC, datetime

from confluent_kafka import Producer
from sqlalchemy import select

from app.core.db.unit_of_work import UnitOfWork
from app.payment.outbox import PaymentOutboxEvent

logger = logging.getLogger(__name__)

TOPIC = "payment-events"


async def publish_pending_payment_events(uow: UnitOfWork, producer: Producer) -> None:
    """Publish every unpublished row, oldest first, then mark only the ones
    Kafka actually confirmed -- at-least-once, same reasoning as Booking's
    relay: an unmarked row just gets retried next cycle. Keyed by
    reservation_id, so one reservation's events stay in order."""
    result = await uow.session.execute(
        select(PaymentOutboxEvent)
        .where(PaymentOutboxEvent.published_at.is_(None))
        .order_by(PaymentOutboxEvent.created_at)
    )
    pending = result.scalars().all()
    if not pending:
        return

    failures: list[tuple[PaymentOutboxEvent, Exception]] = []

    def _on_delivery(err: Exception | None, _msg: object, event: PaymentOutboxEvent) -> None:
        if err is not None:
            failures.append((event, err))

    for event in pending:
        producer.produce(
            TOPIC,
            key=str(event.reservation_id),
            value=json.dumps(event.payload),
            # event=event: early binding, see core/outbox/relay.py
            on_delivery=lambda err, msg, event=event: _on_delivery(err, msg, event),
        )

    producer.flush(timeout=30)

    for event, err in failures:
        logger.error("Failed to publish payment event %s (%s): %s", event.id, event.event_type, err)

    now = datetime.now(UTC)
    failed_ids = {event.id for event, _ in failures}
    for event in pending:
        if event.id not in failed_ids:
            event.published_at = now
