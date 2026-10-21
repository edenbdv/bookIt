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

import json
import smtplib
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from notification import consumer
from notification.__main__ import _HANDLERS
from notification.models import ProcessedNotificationEvent
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox import relay
from app.core.outbox.models import OutboxEvent
from app.reservation import service as reservation_service
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.schemas import ReservationCreate
from tests.integration.helpers import make_pending_reservation, make_property, make_user

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
    with patch("notification.service._send") as send:
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


async def _consume(kafka, handle, messages: int, dead_letter=None) -> None:
    """Poll the real consumer until `messages` arrived -- handled, or (with a
    dead-letter sink) set aside after failing. Counted either way."""
    arrived = 0

    async def counting(payload: dict) -> None:
        nonlocal arrived
        try:
            await handle(payload)
        finally:
            arrived += 1

    for _ in range(CONSUME_TIMEOUT_POLLS):
        await consumer._process_one(kafka, counting, dead_letter)
        if arrived >= messages:
            return
    raise AssertionError(f"only {arrived} of {messages} message(s) arrived")


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
    notification_db_session: AsyncSession,
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
    # ...and the consumer saved its Inbox row, keyed on that same event --
    # in Notification's own database, not Booking's (BOOK-64 step 4)
    assert (
        await notification_db_session.scalar(
            select(func.count()).select_from(ProcessedNotificationEvent)
        )
        == 1
    )


# BOOK-64 step 3: a new booking goes through the real creation path, so it
# emits BookingCreated (no more Celery .delay()); confirming it emits
# BookingConfirmed. Both wait in the outbox and go out in ONE relay cycle --
# the "relay was down for a while" case. Same topic, same key
# (reservation_id) -> same partition, published oldest-first -> the guest
# gets "received" before "confirmed", never the other way round.
@pytest.mark.anyio
async def test_new_booking_sends_received_then_confirmed_in_order(
    kafka_bootstrap_servers: str,
    app_db: None,
    db_session: AsyncSession,
    notification_db_session: AsyncSession,
    kafka_consumer,
    sent_emails: MagicMock,
) -> None:
    host = await make_user(db_session, "host@test.com")
    guest = await make_user(db_session, "guest@test.com")
    prop = await make_property(db_session, host)
    await db_session.commit()

    check_in = datetime.now(UTC).date() + timedelta(days=30)
    async with UnitOfWork() as uow:
        reservation = await reservation_service.create_reservation_idempotent(
            uow,
            ReservationCreate(
                property_id=prop.id,
                check_in=check_in,
                check_out=check_in + timedelta(days=4),
                guest_count=2,
            ),
            guest.id,
            str(uuid.uuid4()),
        )
    async with UnitOfWork() as uow:
        await reservation_service.confirm_reservation(uow, reservation.id)

    await _run_relay()  # both events, one cycle
    await _consume(kafka_consumer, consumer.make_dispatcher(_HANDLERS), messages=2)

    subjects = [call.kwargs["subject"] for call in sent_emails.call_args_list]
    assert subjects == [
        "Your reservation request for Cabin",
        "Your reservation for Cabin is confirmed",
    ]
    assert all(call.kwargs["to"] == guest.email for call in sent_emails.call_args_list)
    # the received email's hold time survived the ISO-string round trip
    assert " UTC -- it will be released" in sent_emails.call_args_list[0].kwargs["body"]


# the full at-least-once story with a real broker: the same event published
# twice (relay crashed after publishing, before marking) reaches the consumer
# twice -- and the Inbox makes sure the guest still gets one email
@pytest.mark.anyio
async def test_republished_event_sends_only_one_email(
    kafka_bootstrap_servers: str,
    app_db: None,
    db_session: AsyncSession,
    notification_db_session: AsyncSession,
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
        await notification_db_session.scalar(
            select(func.count()).select_from(ProcessedNotificationEvent)
        )
        == 1
    )


def _read_dead_letters(topic: str, expected: int) -> list:
    """Read `expected` messages off the dead-letter topic with a throwaway
    consumer group -- the DLQ is just another Kafka topic."""
    from confluent_kafka import Consumer

    from app.core.config import get_settings

    reader = Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": f"dlq-reader-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
        }
    )
    reader.subscribe([topic])
    found = []
    try:
        for _ in range(CONSUME_TIMEOUT_POLLS):
            msg = reader.poll(1.0)
            if msg is not None and not msg.error():
                found.append(msg)
                if len(found) >= expected:
                    break
    finally:
        reader.close()
    return found


# BOOK-64 step 3d: an email that fails for good (a refused address) is set
# aside on booking-events.dlq with its error, and the next guest's email
# still goes out -- one bad message no longer stops the queue
@pytest.mark.anyio
async def test_failing_event_goes_to_dead_letter_and_the_queue_keeps_moving(
    kafka_bootstrap_servers: str,
    app_db: None,
    db_session: AsyncSession,
    notification_db_session: AsyncSession,
    kafka_consumer,
    sent_emails: MagicMock,
) -> None:
    host = await make_user(db_session, "host@test.com")
    bad_guest = await make_user(db_session, "bounces@test.com")
    good_guest = await make_user(db_session, "dana@test.com")
    prop = await make_property(db_session, host)
    check_in = datetime.now(UTC).date() + timedelta(days=30)
    bad, good = (
        Reservation(
            property_id=prop.id,
            guest_id=guest.id,
            check_in=check_in + timedelta(days=offset),
            check_out=check_in + timedelta(days=offset + 2),
            guest_count=2,
            total_cents=20000,
            status=ReservationStatus.PENDING,
        )
        for guest, offset in ((bad_guest, 0), (good_guest, 10))
    )
    db_session.add_all([bad, good])
    await db_session.commit()
    for reservation in (bad, good):  # bad first -- it's ahead in the queue
        async with UnitOfWork() as uow:
            await reservation_service.confirm_reservation(uow, reservation.id)

    def smtp(to: str, subject: str, body: str) -> None:
        if to == "bounces@test.com":
            raise smtplib.SMTPRecipientsRefused({to: (550, b"no such user")})

    sent_emails.side_effect = smtp

    await _run_relay()
    await _consume(
        kafka_consumer,
        consumer.make_dispatcher(_HANDLERS),
        messages=2,
        dead_letter=consumer.make_dead_letter(consumer.make_producer()),
    )

    # the queue kept moving: the second guest still got their email
    assert [c.kwargs["to"] for c in sent_emails.call_args_list] == [
        "bounces@test.com",  # tried once -- refused addresses aren't retried
        "dana@test.com",
    ]
    # ...and the failed one is on the DLQ, original bytes, error in headers
    [dead] = _read_dead_letters(consumer.dead_letter_topic(), expected=1)
    assert json.loads(dead.value())["reservation_id"] == str(bad.id)
    headers = {key: value.decode() for key, value in dead.headers()}
    assert headers["error_type"] == "SMTPRecipientsRefused"
    # only the email that went out is marked processed
    assert (
        await notification_db_session.scalar(
            select(func.count()).select_from(ProcessedNotificationEvent)
        )
        == 1
    )
