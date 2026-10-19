"""BOOK-51: the Kafka poll loop for BookingConfirmed/BookingCancelled.

Not a Celery task -- own process (worker-decisions.md §12).

`handle` is a parameter, not an import of notification/service.py, so the
loop is testable with a fake. app/notification_consumer.py wires the real
one via make_dispatcher() below.
"""

import asyncio
import json
import logging
import smtplib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from confluent_kafka import Consumer, Producer

from app.core.config import get_settings
from app.notification.db import get_notification_engine, notification_session
from app.notification.models import ProcessedNotificationEvent

logger = logging.getLogger(__name__)

TOPIC = "booking-events"
GROUP_ID = "notifications"

# BOOK-64: a handler gets the event payload and nothing else -- everything an
# email needs travels in the event, so no handler reads Booking's tables
EventHandler = Callable[[dict], Awaitable[None]]

# BOOK-64: the retry policy the Celery email task had (BOOK-47), now in the
# consumer. Transient only (retry-basics.md §1).
TRANSIENT_ERRORS = (
    OSError,
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPHeloError,
)
# Checked FIRST, because every smtplib error subclasses OSError -- listing
# OSError above would otherwise retry these too. (The old Celery policy had
# exactly that bug: its comment excluded refused addresses, but OSError in
# autoretry_for caught them anyway.) None of these fix themselves on retry.
PERMANENT_ERRORS = (
    smtplib.SMTPRecipientsRefused,
    smtplib.SMTPSenderRefused,
    smtplib.SMTPAuthenticationError,
    smtplib.SMTPNotSupportedError,
)
# seconds before each retry: 4 retries, ~15s in all
RETRY_DELAYS = (1.0, 2.0, 4.0, 8.0)


async def _with_retry(handler: EventHandler, payload: dict) -> None:
    """Run `handler`, retrying transient errors with growing waits. A
    permanent error, or a transient one that outlasts every retry, is raised."""
    for attempt, delay in enumerate((*RETRY_DELAYS, None), start=1):
        try:
            await handler(payload)
            return
        except PERMANENT_ERRORS:
            raise
        except TRANSIENT_ERRORS as exc:
            if delay is None:
                raise
            logger.warning(
                "Event %s: transient error on attempt %d (%s), retrying in %.0fs",
                payload.get("event_id"),
                attempt,
                exc,
                delay,
            )
            await asyncio.sleep(delay)


def make_consumer() -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": GROUP_ID,
            # manual commit, after handling succeeds -- see _process_one
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


def make_dispatcher(email_handlers: dict[str, EventHandler]) -> Callable[[dict], Awaitable[None]]:
    """Wrap `email_handlers` with the Inbox-pattern dedup check (§13) -- one
    "already handled?" gate shared by every event, not per-handler.

    Marks processed *after* sending, not before -- same trade as BOOK-50's
    relay: risk a rare duplicate over silently losing the email.
    """

    async def dispatch(payload: dict) -> None:
        # BOOK-64: an event type with no email is skipped, not a crash. A
        # KeyError here would leave the message uncommitted, redelivered
        # forever, and every message behind it stuck -- a poison message.
        # Booking may publish events Notification doesn't care about (or
        # doesn't know *yet* -- e.g. a new type rolled out before its
        # handler); "not mine" is a normal answer.
        handler = email_handlers.get(payload["event_type"])
        if handler is None:
            logger.warning(
                "Skipping event %s: no email for event type %r",
                payload.get("event_id"),
                payload["event_type"],
            )
            return

        event_id = uuid.UUID(payload["event_id"])
        # BOOK-64: Notification's own session (notification/db.py), not the
        # monolith's UnitOfWork -- it only ever touches its own Inbox table
        async with notification_session() as session:
            already_processed = await session.get(ProcessedNotificationEvent, event_id)
            if already_processed is not None:
                return
            # retried in place, still before marking processed -- a retry that
            # succeeds is marked like any other send
            await _with_retry(handler, payload)
            session.add(ProcessedNotificationEvent(event_id=event_id))

    return dispatch


# BOOK-64: where a message goes once handling has failed for good -- gets the
# original message bytes and the error. Kafka has no broker-side dead
# lettering (BOOK-48), so this is hand-rolled: a separate topic.
DeadLetter = Callable[[bytes, Exception], Awaitable[None]]


def dead_letter_topic() -> str:
    # read at call time, so a patched TOPIC (tests) moves the DLQ with it
    return f"{TOPIC}.dlq"


def make_dead_letter(producer: Producer) -> DeadLetter:
    """The real sink: publish to `<topic>.dlq` and wait for Kafka to confirm.

    The value is the original message, byte for byte -- so it can be
    inspected, or replayed onto the main topic once the cause is fixed. Why
    it failed travels in headers, not wrapped around the value.
    """

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


def make_producer() -> Producer:
    return Producer({"bootstrap.servers": get_settings().kafka_bootstrap_servers})


async def _process_one(
    consumer: Consumer,
    handle: Callable[[dict], Awaitable[None]],
    dead_letter: DeadLetter | None = None,
) -> None:
    # poll() is blocking -- to_thread() so it doesn't stall the only event loop
    msg = await asyncio.to_thread(consumer.poll, 1.0)
    if msg is None:
        return
    if msg.error():
        logger.error("Kafka consumer error: %s", msg.error())
        return
    try:
        await handle(json.loads(msg.value()))
    except Exception as exc:
        # failed for good: permanent error, retries exhausted, or not even
        # valid JSON. Without a dead-letter sink, raise -- the old behavior.
        if dead_letter is None:
            raise
        # Set it aside, then commit so the queue keeps moving instead of one
        # message blocking every message behind it. If the dead-letter
        # publish itself fails, that raises before the commit: the message
        # stays uncommitted and is redelivered -- never silently dropped.
        logger.exception("Dead-lettering message at offset %s", msg.offset())
        await dead_letter(msg.value(), exc)
    await asyncio.to_thread(consumer.commit, msg)


async def _run(
    consumer: Consumer, handle: Callable[[dict], Awaitable[None]], dead_letter: DeadLetter
) -> None:
    consumer.subscribe([TOPIC])
    try:
        while True:
            await _process_one(consumer, handle, dead_letter)
    finally:
        consumer.close()
        await get_notification_engine().dispose()


def run(
    consumer: Consumer, handle: Callable[[dict], Awaitable[None]], dead_letter: DeadLetter
) -> None:
    """One asyncio.run() for the process's whole lifetime, not one per
    message -- the engine is created once and reused for every message
    instead of torn down and rebuilt each time (worker-decisions.md §12).
    """
    asyncio.run(_run(consumer, handle, dead_letter))
