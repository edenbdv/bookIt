"""BOOK-64: the booking-events pipeline end to end, against a real Postgres
and a real Kafka -- what tests/notification/'s unit tests (all fakes) can't
see: the relay actually publishing, the message actually crossing the
broker, the consumer group actually receiving it, the payload surviving
JSON in and out, and the Inbox row actually being saved.

    confirm_reservation -> outbox row -> relay -> Kafka -> consumer -> email

Only SMTP is faked (_send patched): what's under test is the pipeline, and
that the right email *would* go out. Flow: docs/jobs/booking-events-flow.md.

These are the guard rails for BOOK-64 itself: the event payload becomes a
contract between two services, and the consumer moves out of the monolith.
"""

import uuid
from collections.abc import Iterator
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox import relay
from app.core.outbox.models import OutboxEvent
from app.notification import consumer
from app.notification.models import ProcessedNotificationEvent
from app.notification_consumer import _HANDLERS
from app.reservation import service as reservation_service
from tests.integration.helpers import make_pending_reservation

# first poll includes joining the consumer group and the topic being
# auto-created -- seconds, not milliseconds
CONSUME_TIMEOUT_POLLS = 30


@pytest.fixture(autouse=True)
def _isolated_topic() -> Iterator[str]:
    """A fresh topic per test, for both the relay and the consumer. Kafka
    keeps messages and committed offsets across tests -- TRUNCATE can't reach
    them -- so a shared topic would leak one test's events into the next."""
    topic = f"booking-events-{uuid.uuid4().hex[:8]}"
    with patch.object(relay, "_TOPIC", topic), patch.object(consumer, "TOPIC", topic):
        yield topic


@pytest.fixture
def sent_emails() -> Iterator[MagicMock]:
    with patch("app.notification.service._send") as send:
        yield send


async def _confirm_a_reservation(db_session: AsyncSession):
    reservation, guest = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:
        await reservation_service.confirm_reservation(uow, reservation.id)
    return reservation, guest


async def _run_relay() -> None:
    producer = relay.make_producer()
    async with UnitOfWork() as uow:
        await relay.publish_pending_events(uow, producer)


async def _consume(kafka, handle, messages: int) -> None:
    """Poll the real consumer until `messages` were handled and committed."""
    handled = 0

    async def counting(payload: dict) -> None:
        nonlocal handled
        await handle(payload)
        handled += 1

    for _ in range(CONSUME_TIMEOUT_POLLS):
        await consumer._process_one(kafka, counting)
        if handled >= messages:
            return
    raise AssertionError(f"only {handled} of {messages} message(s) arrived")


@pytest.fixture
def kafka_consumer(_isolated_topic: str) -> Iterator[object]:
    kafka = consumer.make_consumer()  # real config: group "notifications", manual commit
    kafka.subscribe([_isolated_topic])
    yield kafka
    kafka.close()


@pytest.mark.anyio
async def test_confirmed_reservation_reaches_the_guest_through_kafka(
    kafka_bootstrap_servers: str,
    app_db: None,
    db_session: AsyncSession,
    kafka_consumer,
    sent_emails: MagicMock,
) -> None:
    reservation, guest = await _confirm_a_reservation(db_session)

    await _run_relay()
    await _consume(kafka_consumer, consumer.make_dispatcher(_HANDLERS), messages=1)

    sent_emails.assert_called_once()
    email = sent_emails.call_args.kwargs
    assert email["to"] == guest.email
    assert email["subject"] == "Your reservation for Cabin is confirmed"

    # the relay marked the outbox row published...
    [published_at] = (
        await db_session.scalars(
            select(OutboxEvent.published_at).where(OutboxEvent.reservation_id == reservation.id)
        )
    ).all()
    assert published_at is not None
    # ...and the consumer saved its Inbox row, keyed on that same event
    assert (
        await db_session.scalar(select(func.count()).select_from(ProcessedNotificationEvent)) == 1
    )


# the full at-least-once story with a real broker: the same event published
# twice (relay crashed after publishing, before marking) reaches the consumer
# twice -- and the Inbox makes sure the guest still gets one email
@pytest.mark.anyio
async def test_republished_event_sends_only_one_email(
    kafka_bootstrap_servers: str,
    app_db: None,
    db_session: AsyncSession,
    kafka_consumer,
    sent_emails: MagicMock,
) -> None:
    reservation, _ = await _confirm_a_reservation(db_session)
    await _run_relay()

    # simulate the relay crash: the row is "unpublished" again, so the next
    # cycle publishes the very same event (same event_id) a second time
    await db_session.execute(
        update(OutboxEvent)
        .where(OutboxEvent.reservation_id == reservation.id)
        .values(published_at=None)
    )
    await db_session.commit()
    await _run_relay()

    await _consume(kafka_consumer, consumer.make_dispatcher(_HANDLERS), messages=2)

    sent_emails.assert_called_once()
    assert (
        await db_session.scalar(select(func.count()).select_from(ProcessedNotificationEvent)) == 1
    )
