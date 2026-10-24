"""BOOK-65: Payment's own relay -- publishes `payment_outbox_events` rows to
the `payment-events` topic. Same polling relay as core/outbox/relay.py
(BOOK-50, worker-decisions.md §11), kept as its own copy on purpose: it
moves out with Payment when Payment becomes its own service, while
Booking's relay stays behind with Booking's outbox.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

from confluent_kafka import Producer
from sqlalchemy import select

from payment.db import PaymentUnitOfWork
from payment.models import PaymentOutboxEvent

logger = logging.getLogger(__name__)

TOPIC = "payment-events"


async def publish_pending_payment_events(uow: PaymentUnitOfWork, producer: Producer) -> None:
    """Publish every unpublished row, oldest first, then mark only the ones
    Kafka actually confirmed -- at-least-once: an unmarked row just gets
    retried next cycle. Keyed by reservation_id, so one reservation's
    events stay in order.

    "Confirmed" means a delivery callback with no error, not "no error
    yet": a message still waiting when flush() times out (topic missing,
    broker slow or down) has had no callback at all, and must stay
    unmarked. Retrying it may duplicate it on the topic -- fine, consumers
    dedup on event_id; a lost event is not."""
    result = await uow.session.execute(
        select(PaymentOutboxEvent)
        .where(PaymentOutboxEvent.published_at.is_(None))
        .order_by(PaymentOutboxEvent.created_at)
    )
    pending = result.scalars().all()
    if not pending:
        return

    delivered: set[uuid.UUID] = set()

    def _on_delivery(err: Exception | None, _msg: object, event: PaymentOutboxEvent) -> None:
        if err is None:
            delivered.add(event.id)  # the broker stored it
        else:
            logger.error(
                "Failed to publish payment event %s (%s): %s", event.id, event.event_type, err
            )

    for event in pending:
        producer.produce(
            TOPIC,
            key=str(event.reservation_id),
            value=json.dumps(event.payload),
            # event=event: early binding, see core/outbox/relay.py
            on_delivery=lambda err, msg, event=event: _on_delivery(err, msg, event),
        )

    # callbacks only fire inside flush()/poll(); returns how many are still waiting
    remaining = producer.flush(timeout=30)
    if remaining:
        logger.warning("%d payment event(s) not confirmed yet -- retried next run", remaining)

    now = datetime.now(UTC)
    for event in pending:
        if event.id in delivered:
            event.published_at = now
