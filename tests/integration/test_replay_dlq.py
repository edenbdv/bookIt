"""BOOK-68: scripts/replay_dlq.py against a real Kafka -- a dead letter goes back
to its main topic, keyed by reservation again, and only once."""

import json
import uuid

import pytest
from confluent_kafka import Consumer

from app.core.config import get_settings
from app.core.outbox.relay import make_producer
from scripts import replay_dlq

CONSUME_TIMEOUT_POLLS = 30


def _dead_letter(dlq: str, payload: dict) -> None:
    # the way make_dead_letter writes one: no key, the reason in headers
    producer = make_producer()
    producer.produce(
        dlq,
        value=json.dumps(payload).encode(),
        headers={"error_type": "OperationalError", "error": "connection refused"},
    )
    producer.flush(10)


def _read(topic: str) -> list:
    reader = Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": f"reader-{uuid.uuid4().hex[:8]}",
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


# async only for the suite's autouse fixtures (table truncation) -- the script is sync
@pytest.mark.anyio
async def test_a_dead_letter_is_replayed_to_its_topic_once(kafka_bootstrap_servers: str) -> None:
    topic = f"payment-events-{uuid.uuid4().hex[:8]}"
    dlq = f"{topic}.dlq"
    reservation_id = str(uuid.uuid4())
    payload = {"event_id": str(uuid.uuid4()), "reservation_id": reservation_id}
    _dead_letter(dlq, payload)

    assert [m["reservation_id"] for m in replay_dlq.list_dead_letters(dlq)] == [reservation_id]
    assert replay_dlq.replay(dlq) == 1

    [msg] = _read(topic)
    assert json.loads(msg.value()) == payload  # byte for byte, as it failed
    assert msg.key().decode() == reservation_id  # ordering per reservation again
    assert dict(msg.headers())["replayed_from"] == dlq.encode()
    # already replayed -- a second run sends nothing
    assert replay_dlq.replay(dlq) == 0


@pytest.mark.anyio
async def test_the_target_is_the_dlq_name_without_its_suffix() -> None:
    assert replay_dlq.target_topic("payment-events.dlq") == "payment-events"
    assert replay_dlq.target_topic("booking-events.payment.dlq") == "booking-events"
    assert replay_dlq.target_topic("booking-events.dlq") == "booking-events"
