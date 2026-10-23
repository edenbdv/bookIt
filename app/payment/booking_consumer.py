"""BOOK-65: Payment's Kafka consumer for booking-events. Run with:

    uv run python -m app.payment.booking_consumer

Same loop as Booking's payment_consumer.py, kept as its own copy: it moves out
with Payment. Its own group ("payment") gets every booking-events message,
independently of Notification's "notifications".
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
from app.payment.booking_events import handle_booking_event

logger = logging.getLogger(__name__)

TOPIC = "booking-events"
GROUP_ID = "payment"

DeadLetter = Callable[[bytes, Exception], Awaitable[None]]


def dead_letter_topic() -> str:
    # its own DLQ, not Notification's booking-events.dlq -- two groups read this
    # topic, and a shared DLQ wouldn't say whose failure it was. Read at call
    # time, so a patched TOPIC (tests) moves the DLQ with it.
    return f"{TOPIC}.payment.dlq"


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
    """Publish to the DLQ and wait for Kafka to confirm -- the original bytes
    (replayable once fixed), why it failed in headers."""

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
        # one UnitOfWork per message -- the Inbox row commits with the PENDING refund
        async with UnitOfWork() as uow:
            await handle_booking_event(uow, json.loads(msg.value()))
    except Exception as exc:
        # set aside and keep the queue moving; a failed DLQ publish raises
        # before the commit -- redelivered, never silently dropped
        logger.exception("Dead-lettering booking event at offset %s", msg.offset())
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
