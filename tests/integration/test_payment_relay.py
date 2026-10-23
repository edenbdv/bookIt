"""BOOK-65: Payment's relay against a real Postgres and a real Kafka --

    charge resolves -> payment_outbox_events row -> relay -> payment-events

No consumer yet (Booking starts reading payment-events in BOOK-68's core),
so a throwaway consumer group reads the topic back to check what landed.
"""

import json
import uuid
from collections.abc import Iterator
from unittest.mock import patch

import pytest
from confluent_kafka import Consumer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db.unit_of_work import UnitOfWork
from app.core.outbox.relay import make_producer
from app.payment import relay
from app.payment.outbox import PaymentOutboxEvent
from app.payment.provider import PaymentOutcome
from app.payment.saga import create_payment_and_react
from app.payment.schemas import PaymentCreate
from tests.integration.helpers import make_pending_reservation

# first poll includes joining the group and the topic being auto-created
CONSUME_TIMEOUT_POLLS = 30


@pytest.fixture(autouse=True)
def _isolated_topic() -> Iterator[str]:
    # a fresh topic per test -- Kafka keeps messages across tests, TRUNCATE can't reach them
    topic = f"payment-events-{uuid.uuid4().hex[:8]}"
    with patch.object(relay, "TOPIC", topic):
        yield topic


async def _run_relay() -> None:
    async with UnitOfWork() as uow:
        await relay.publish_pending_payment_events(uow, make_producer())


def _read(topic: str, expected: int) -> list:
    reader = Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": f"payment-events-reader-{uuid.uuid4().hex[:8]}",
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


@pytest.mark.anyio
async def test_a_succeeded_charge_reaches_payment_events(
    app_db: None,
    kafka_bootstrap_servers: str,
    db_session: AsyncSession,
    _isolated_topic: str,
) -> None:
    reservation, guest = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:
        payment = await create_payment_and_react(
            uow,
            PaymentCreate(reservation_id=reservation.id),
            guest.id,
            str(uuid.uuid4()),
            force=PaymentOutcome.SUCCESS,
        )
        payment_id = payment.id

    await _run_relay()

    [msg] = _read(_isolated_topic, expected=1)
    assert msg.key().decode() == str(reservation.id)  # partition key
    payload = json.loads(msg.value())
    assert payload["event_type"] == "payment_succeeded"
    assert payload["payment_id"] == str(payment_id)
    assert payload["amount_cents"] == reservation.total_cents
    # marked published, so the next run doesn't send it again
    published_at = await db_session.scalar(select(PaymentOutboxEvent.published_at))
    assert published_at is not None


# a second run finds nothing left to send -- no duplicate on the topic
@pytest.mark.anyio
async def test_running_the_relay_again_publishes_nothing_new(
    app_db: None,
    kafka_bootstrap_servers: str,
    db_session: AsyncSession,
    _isolated_topic: str,
) -> None:
    reservation, guest = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:
        await create_payment_and_react(
            uow,
            PaymentCreate(reservation_id=reservation.id),
            guest.id,
            str(uuid.uuid4()),
            force=PaymentOutcome.FAILURE,
        )

    await _run_relay()
    await _run_relay()

    # ask for 2 -- only 1 may ever arrive
    messages = _read(_isolated_topic, expected=2)
    assert [json.loads(m.value())["event_type"] for m in messages] == ["payment_failed"]


class _NeverConfirmsProducer:
    """Accepts messages but never calls their delivery callback -- what a
    real producer does while the topic is missing or the broker is slow or
    down: no success, no failure, just still waiting when flush() times out."""

    def __init__(self) -> None:
        self.produced = 0

    def produce(self, *_args: object, **_kwargs: object) -> None:
        self.produced += 1

    def flush(self, timeout: float) -> int:
        return self.produced  # all still waiting


# the bug this guards against: "no error yet" was treated as published, so a
# message still waiting at flush's timeout was marked and lost for good
@pytest.mark.anyio
async def test_an_unconfirmed_event_stays_unpublished_and_is_retried(
    app_db: None, db_session: AsyncSession
) -> None:
    reservation, guest = await make_pending_reservation(db_session)
    async with UnitOfWork() as uow:
        await create_payment_and_react(
            uow,
            PaymentCreate(reservation_id=reservation.id),
            guest.id,
            str(uuid.uuid4()),
            force=PaymentOutcome.SUCCESS,
        )
    producer = _NeverConfirmsProducer()

    async with UnitOfWork() as uow:
        await relay.publish_pending_payment_events(uow, producer)

    assert producer.produced == 1
    published_at = await db_session.scalar(select(PaymentOutboxEvent.published_at))
    assert published_at is None  # still pending -- the next run sends it again
