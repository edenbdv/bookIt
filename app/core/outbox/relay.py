"""BOOK-50's relay -- reads `outbox_events` rows nothing has published yet
and hands them to Kafka. A polling relay, not CDC (worker-decisions.md §11).

Run on a schedule by Celery beat (`app/core/jobs/celery_app.py`), executed
by the existing worker (`app/worker.py`) -- not a separate process.
"""

import json
import logging
from datetime import UTC, datetime

from confluent_kafka import Producer
from sqlalchemy import select

from app.core.config import get_settings
from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox.models import OutboxEvent

logger = logging.getLogger(__name__)

_TOPIC = "booking-events"


def make_producer() -> Producer:
    return Producer({"bootstrap.servers": get_settings().kafka_bootstrap_servers})


async def publish_pending_events(uow: UnitOfWork, producer: Producer) -> None:
    """Publish every unpublished row, oldest first, then mark only the ones
    Kafka actually confirmed.

    Creation order matters here, not just tidiness: publishing out of order
    would undo the per-reservation ordering guarantee BOOK-49's partition
    key depends on (outbox-basics.md §6). Marking only *confirmed* rows
    (never the ones that failed to send) is what keeps this at-least-once
    instead of silently dropping an event that never actually left the
    process (outbox-basics.md §1) -- an unmarked row just gets retried next
    cycle.
    """
    result = await uow.session.execute(
        select(OutboxEvent)
        .where(OutboxEvent.published_at.is_(None))
        .order_by(OutboxEvent.created_at)
    )
    pending = result.scalars().all()
    if not pending:
        return

    failures: list[tuple[OutboxEvent, Exception]] = []

    def _on_delivery(err: Exception | None, _msg: object, event: OutboxEvent) -> None:
        if err is not None:
            failures.append((event, err))

    for event in pending:
        producer.produce(
            _TOPIC,
            key=str(event.reservation_id),
            value=json.dumps(event.payload),
            # event=event: forces early binding -- the callback only runs
            # later, during flush(), by which point a plain closure would
            # see whatever `event` was on the loop's last iteration
            on_delivery=lambda err, msg, event=event: _on_delivery(err, msg, event),
        )

    producer.flush(timeout=30)

    for event, err in failures:
        logger.error("Failed to publish outbox event %s (%s): %s", event.id, event.event_type, err)

    now = datetime.now(UTC)
    failed_ids = {event.id for event, _ in failures}
    for event in pending:
        if event.id not in failed_ids:
            event.published_at = now
