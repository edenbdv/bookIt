"""BOOK-65: Payment's Kafka consumer for booking-events. Run with:

    uv run python -m payment.booking_consumer

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
from sqlalchemy.exc import InterfaceError, OperationalError

from payment.booking_events import handle_booking_event
from payment.config import get_payment_settings
from payment.db import PaymentUnitOfWork, get_payment_engine

logger = logging.getLogger(__name__)

TOPIC = "booking-events"
GROUP_ID = "payment"

DeadLetter = Callable[[bytes, Exception], Awaitable[None]]

# BOOK-68: Payment's own DB having a moment -- worth another try, or a refund
# would sit in the DLQ. Anything else fails the same way every time: straight
# there. Same copy as Booking's payment_consumer.py.
TRANSIENT_ERRORS = (OperationalError, InterfaceError, OSError, TimeoutError)
# seconds before each retry: 3 tries, ~3s in all
RETRY_DELAYS = (1.0, 2.0)


async def _with_retry(attempt_once: Callable[[], Awaitable[None]]) -> None:
    """Run `attempt_once`, retrying transient errors with growing waits. A
    permanent error, or a transient one that outlasts every retry, is raised."""
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            await attempt_once()
            return
        except TRANSIENT_ERRORS as exc:
            if delay is None:
                raise
            logger.warning(
                "Transient error on attempt %d (%s), retrying in %.0fs", attempt, exc, delay
            )
            await asyncio.sleep(delay)


def dead_letter_topic() -> str:
    # its own DLQ, not Notification's booking-events.dlq -- two groups read this
    # topic, and a shared DLQ wouldn't say whose failure it was. Read at call
    # time, so a patched TOPIC (tests) moves the DLQ with it.
    return f"{TOPIC}.payment.dlq"


def make_consumer() -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": get_payment_settings().kafka_bootstrap_servers,
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

    async def attempt_once() -> None:
        # a fresh PaymentUnitOfWork per try -- the Inbox row commits with the
        # PENDING refund, so a retry after that commit finds it and stops
        async with PaymentUnitOfWork() as uow:
            await handle_booking_event(uow, json.loads(msg.value()))

    try:
        await _with_retry(attempt_once)
    except Exception as exc:
        # out of retries, or not worth one: set aside and keep the queue
        # moving; a failed DLQ publish raises before the commit --
        # redelivered, never silently dropped
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
        await get_payment_engine().dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    producer = Producer({"bootstrap.servers": get_payment_settings().kafka_bootstrap_servers})
    # one asyncio.run() for the process's whole lifetime, not one per message
    asyncio.run(_run(make_consumer(), make_dead_letter(producer)))


if __name__ == "__main__":
    main()
