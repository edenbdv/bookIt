"""BOOK-51: the Kafka poll loop for BookingConfirmed/BookingCancelled.

Not a Celery task -- own process, own docker-compose service, a
confluent_kafka.Consumer in group "notifications" (worker-decisions.md §12).

`run()`/`_process_one()` take `handle` as a parameter instead of importing
notification/service.py directly, so the poll/parse/commit loop is
unit-testable with a fake handler. app/notification_consumer.py assembles
the real one via make_dispatcher() below.
"""

import asyncio
import json
import logging
import uuid
from collections.abc import Awaitable, Callable

from confluent_kafka import Consumer

from app.core.config import get_settings
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
            # committed only after handling succeeds (_process_one below) --
            # auto-commit risks losing a message instead of just duplicating it
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


def _process_one(consumer: Consumer, handle: Callable[[dict], Awaitable[None]]) -> None:
    msg = consumer.poll(1.0)
    if msg is None:
        return
    if msg.error():
        logger.error("Kafka consumer error: %s", msg.error())
        return
    payload = json.loads(msg.value())
    asyncio.run(handle(payload))
    consumer.commit(msg)


def run(consumer: Consumer, handle: Callable[[dict], Awaitable[None]]) -> None:
    """Poll forever, one message at a time -- preserves BOOK-49's per-reservation
    ordering all the way through to the email that gets sent.
    """
    consumer.subscribe([TOPIC])
    try:
        while True:
            _process_one(consumer, handle)
    finally:
        consumer.close()
