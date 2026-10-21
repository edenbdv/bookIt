"""BOOK-65: Booking's Kafka consumer for payment-events. Run with:

    uv run python -m app.reservation.payment_consumer

Own process, not Celery and not inside the API -- same call Notification's
consumer made (worker-decisions.md §12). Same loop shape too: poll one
message, handle it, commit the offset only after handling is done. What it
does with each message is handle_payment_event (payment_events.py).
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from confluent_kafka import Consumer, Producer

from app.core.config import get_settings
from app.core.db.engine import get_engine
from app.core.db.unit_of_work import UnitOfWork
from app.reservation.payment_events import handle_payment_event

logger = logging.getLogger(__name__)

TOPIC = "payment-events"
# its own group -- independent of "notifications" on booking-events
GROUP_ID = "booking"

# where a message goes once handling has failed -- gets the original bytes
# and the error. Hand-rolled, Kafka has no broker-side dead lettering (BOOK-48).
DeadLetter = Callable[[bytes, Exception], Awaitable[None]]


def dead_letter_topic() -> str:
    # read at call time, so a patched TOPIC (tests) moves the DLQ with it
    return f"{TOPIC}.dlq"


def make_consumer() -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": GROUP_ID,
            # manual commit, after handling -- see _process_one
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


def make_dead_letter(producer: Producer) -> DeadLetter:
    """Publish to `payment-events.dlq` and wait for Kafka to confirm. Same
    sink as Notification's: the original message byte for byte (so it can
    be replayed once fixed), why it failed in headers."""

    async def dead_letter(value: bytes, error: Exception) -> None:
        failures: list[object] = []
        producer.produce(
            dead_letter_topic(),
            value=value,
            headers={
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
                "failed_at": datetime.now(UTC).isoformat(),
            },
            on_delivery=lambda err, _msg: failures.append(err) if err else None,
        )
        # blocking -- to_thread so it doesn't stall the event loop
        await asyncio.to_thread(producer.flush, 10)
        if failures:
            # raising here keeps the original message uncommitted (_process_one)
            raise RuntimeError(f"dead-letter publish failed: {failures[0]}")

    return dead_letter


async def _process_one(consumer: Consumer, dead_letter: DeadLetter) -> None:
    # poll() is blocking -- to_thread() so it doesn't stall the only event loop
    msg = await asyncio.to_thread(consumer.poll, 1.0)
    if msg is None:
        return
    if msg.error():
        logger.error("Kafka consumer error: %s", msg.error())
        return
    try:
        # one UnitOfWork per message: the Inbox row and the confirm/cancel
        # commit together, or roll back together
        async with UnitOfWork() as uow:
            await handle_payment_event(uow, json.loads(msg.value()))
    except Exception as exc:
        # no retry loop, unlike Notification's: there's no flaky external
        # call here, only Booking's own DB. Set it aside and keep the queue
        # moving; if the dead-letter publish itself fails, that raises
        # before the commit -- redelivered, never silently dropped.
        logger.exception("Dead-lettering payment event at offset %s", msg.offset())
        await dead_letter(msg.value(), exc)
    await asyncio.to_thread(consumer.commit, msg)


async def _run(consumer: Consumer, dead_letter: DeadLetter) -> None:
    consumer.subscribe([TOPIC])
    try:
        while True:
            await _process_one(consumer, dead_letter)
    finally:
        consumer.close()
        await get_engine().dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    producer = Producer({"bootstrap.servers": get_settings().kafka_bootstrap_servers})
    # one asyncio.run() for the process's whole lifetime, not one per message
    asyncio.run(_run(make_consumer(), make_dead_letter(producer)))


if __name__ == "__main__":
    main()
