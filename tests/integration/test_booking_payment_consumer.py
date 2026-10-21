"""BOOK-65: the payment-events pipeline end to end, against a real Postgres
and a real Kafka --

    charge resolves -> payment outbox -> payment relay -> Kafka
        -> Booking's consumer -> reservation confirmed

The charge goes through service.create_payment, not the saga, on purpose:
the saga still confirms by direct call, which would hide whether the
consumer did anything at all.
"""

import json
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from confluent_kafka import Consumer, Producer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox.relay import make_producer
from app.payment import relay
from app.payment.provider import PaymentOutcome
from app.payment.service import create_payment
from app.reservation import payment_consumer
from app.reservation.models import ReservationStatus
from tests.integration.helpers import make_pending_reservation, reservation_status

# first poll includes joining the group and the topic being auto-created
CONSUME_TIMEOUT_POLLS = 30


@pytest.fixture(autouse=True)
def _isolated_topic() -> Iterator[str]:
    # one fresh topic per test, for both the relay and the consumer
    topic = f"payment-events-{uuid.uuid4().hex[:8]}"
    with patch.object(relay, "TOPIC", topic), patch.object(payment_consumer, "TOPIC", topic):
        yield topic


@pytest.fixture
def kafka_consumer(_isolated_topic: str) -> Iterator[Consumer]:
    kafka = payment_consumer.make_consumer()  # real config: group "booking", manual commit
    kafka.subscribe([_isolated_topic])
    yield kafka
    kafka.close()


@pytest.fixture
def dead_letter() -> payment_consumer.DeadLetter:
    return payment_consumer.make_dead_letter(make_producer())


async def _consume_until(kafka: Consumer, dead_letter, done) -> None:
    for _ in range(CONSUME_TIMEOUT_POLLS):
        await payment_consumer._process_one(kafka, dead_letter)
        if await done():
            return
    raise AssertionError("consumer never got there")


def _read_dead_letters(topic: str) -> list:
    reader = Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": f"dlq-reader-{uuid.uuid4().hex[:8]}",
            "auto.offset.reset": "earliest",
        }
    )
    reader.subscribe([topic])
    try:
        for _ in range(CONSUME_TIMEOUT_POLLS):
            msg = reader.poll(1.0)
            if msg is not None and not msg.error():
                return [msg]
    finally:
        reader.close()
    return []


@pytest.mark.anyio
async def test_a_succeeded_charge_confirms_the_reservation_through_kafka(
    app_db: None,
    kafka_bootstrap_servers: str,
    db_session: AsyncSession,
    kafka_consumer: Consumer,
    dead_letter,
) -> None:
    reservation, guest = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:  # commits the SUCCEEDED status + its outbox row
        await create_payment(uow, reservation.id, guest.id, force=PaymentOutcome.SUCCESS)
    assert await reservation_status(db_session, reservation) == ReservationStatus.PENDING

    async with UnitOfWork() as uow:
        await relay.publish_pending_payment_events(uow, make_producer())

    async def confirmed() -> bool:
        return await reservation_status(db_session, reservation) == ReservationStatus.CONFIRMED

    await _consume_until(kafka_consumer, dead_letter, confirmed)


# one bad message must not block the queue: it's set aside on the DLQ, and
# the good message behind it is still handled
@pytest.mark.anyio
async def test_a_bad_message_is_dead_lettered_and_the_next_one_still_goes_through(
    app_db: None,
    kafka_bootstrap_servers: str,
    db_session: AsyncSession,
    kafka_consumer: Consumer,
    dead_letter,
    _isolated_topic: str,
) -> None:
    reservation, _ = await make_pending_reservation(db_session)
    good = {
        "event_id": str(uuid.uuid4()),
        "event_type": "payment_failed",
        "reservation_id": str(reservation.id),
        "payment_id": str(uuid.uuid4()),
        "amount_cents": reservation.total_cents,
    }
    producer = Producer({"bootstrap.servers": get_settings().kafka_bootstrap_servers})
    key = str(reservation.id)  # same key -> same partition -> bad one first
    producer.produce(_isolated_topic, key=key, value=b"not json")
    producer.produce(_isolated_topic, key=key, value=json.dumps(good))
    producer.flush(10)

    async def cancelled() -> bool:
        return await reservation_status(db_session, reservation) == ReservationStatus.CANCELLED

    await _consume_until(kafka_consumer, dead_letter, cancelled)

    [dlq_msg] = _read_dead_letters(payment_consumer.dead_letter_topic())
    assert dlq_msg.value() == b"not json"  # the original bytes, replayable
    assert dict(dlq_msg.headers())["error_type"] == b"JSONDecodeError"
