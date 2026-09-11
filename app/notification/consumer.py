"""BOOK-51: the Kafka poll loop for BookingConfirmed/BookingCancelled.

Not a Celery task -- own process (worker-decisions.md §12).

`handle` is a parameter, not an import of notification/service.py, so the
loop is testable with a fake. app/notification_consumer.py wires the real
one via make_dispatcher() below.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from confluent_kafka import Consumer

from app.core.config import get_settings
from app.core.db.engine import get_engine
from app.core.db.unit_of_work import UnitOfWork
from app.notification.models import ProcessedNotificationEvent

logger = logging.getLogger(__name__)

TOPIC = "booking-events"
GROUP_ID = "notifications"

EventHandler = Callable[[UnitOfWork, uuid.UUID], Awaitable[None]]


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
        event_id = uuid.UUID(payload["event_id"])
        async with UnitOfWork() as uow:
            already_processed = await uow.session.get(ProcessedNotificationEvent, event_id)
            if already_processed is not None:
                return
            handler = email_handlers[payload["event_type"]]
            await handler(uow, uuid.UUID(payload["reservation_id"]))
            uow.session.add(ProcessedNotificationEvent(event_id=event_id))

    return dispatch


async def _process_one(consumer: Consumer, handle: Callable[[dict], Awaitable[None]]) -> None:
    # poll() is blocking -- to_thread() so it doesn't stall the only event loop
    msg = await asyncio.to_thread(consumer.poll, 1.0)
    if msg is None:
        return
    if msg.error():
        logger.error("Kafka consumer error: %s", msg.error())
        return
    payload = json.loads(msg.value())
    await handle(payload)
    await asyncio.to_thread(consumer.commit, msg)


async def _run(consumer: Consumer, handle: Callable[[dict], Awaitable[None]]) -> None:
    consumer.subscribe([TOPIC])
    try:
        while True:
            await _process_one(consumer, handle)
    finally:
        consumer.close()
        await get_engine().dispose()


def run(consumer: Consumer, handle: Callable[[dict], Awaitable[None]]) -> None:
    """One asyncio.run() for the process's whole lifetime, not one per
    message -- the engine is created once and reused for every message
    instead of torn down and rebuilt each time (worker-decisions.md §12).
    """
    asyncio.run(_run(consumer, handle))
