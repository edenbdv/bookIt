"""BOOK-68: look at a dead-letter topic, and send its messages back once the cause
is fixed (docs/microservices/microservices-decisions.md §21).

Run with:
    uv run python scripts/replay_dlq.py payment-events.dlq --list
    uv run python scripts/replay_dlq.py payment-events.dlq --replay
    uv run python scripts/replay_dlq.py payment-events.dlq --replay --offset 3

Safe to replay: every consumer has an Inbox, so an event already handled is
skipped, and one that fails again just lands back in the DLQ.
"""

import argparse
import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from confluent_kafka import Consumer, Message, Producer

from app.core.config import get_settings

# --replay's own group: it commits what it sent, so a second run sends only new failures
REPLAY_GROUP = "dlq-replay"
# the first polls include joining the group; after that, this many empty polls = done
MAX_POLLS = 30
IDLE_POLLS = 3


def target_topic(dlq: str) -> str:
    # payment-events.dlq -> payment-events; booking-events.payment.dlq -> booking-events
    return dlq.split(".", 1)[0]


def _consumer(group_id: str) -> Consumer:
    return Consumer(
        {
            "bootstrap.servers": get_settings().kafka_bootstrap_servers,
            "group.id": group_id,
            "enable.auto.commit": False,
            "auto.offset.reset": "earliest",
        }
    )


def _read_all(consumer: Consumer, dlq: str) -> list[Message]:
    consumer.subscribe([dlq])
    found: list[Message] = []
    idle = 0
    for _ in range(MAX_POLLS):
        msg = consumer.poll(1.0)
        if msg is None or msg.error():
            idle += 1
            # done once we hold the partitions and they've gone quiet (empty DLQ too)
            if consumer.assignment() and idle >= IDLE_POLLS:
                break
            continue
        found.append(msg)
        idle = 0
    return found


def _describe(msg: Message) -> dict:
    headers = {k: v.decode() for k, v in (msg.headers() or [])}
    try:
        payload = json.loads(msg.value())
    except ValueError:
        payload = {}
    return {
        "offset": msg.offset(),
        "error_type": headers.get("error_type"),
        "error": headers.get("error"),
        "failed_at": headers.get("failed_at"),
        "event_type": payload.get("event_type"),
        "reservation_id": payload.get("reservation_id"),
    }


def list_dead_letters(dlq: str) -> list[dict]:
    # a throwaway group, nothing committed: always shows everything
    consumer = _consumer(f"dlq-list-{uuid.uuid4().hex[:8]}")
    try:
        return [_describe(m) for m in _read_all(consumer, dlq)]
    finally:
        consumer.close()


def replay(dlq: str, offset: int | None = None) -> int:
    """Send dead letters back to their main topic; returns how many. All not yet
    replayed (committed in REPLAY_GROUP), or only the one at `offset` (a throwaway
    group then -- a later full replay sends it again, which the Inbox absorbs)."""
    group = REPLAY_GROUP if offset is None else f"dlq-one-{uuid.uuid4().hex[:8]}"
    consumer = _consumer(group)
    producer = Producer({"bootstrap.servers": get_settings().kafka_bootstrap_servers})
    topic = target_topic(dlq)
    sent = 0
    try:
        for msg in _read_all(consumer, dlq):
            if offset is not None and msg.offset() != offset:
                continue
            failures: list[object] = []
            producer.produce(
                topic,
                value=msg.value(),
                # the DLQ dropped the key -- back to per-reservation ordering
                key=_describe(msg)["reservation_id"],
                headers={"replayed_from": dlq, "replayed_at": datetime.now(UTC).isoformat()},
                on_delivery=lambda err, _m, f=failures: f.append(err) if err else None,
            )
            producer.flush(10)
            if failures:
                raise RuntimeError(f"replay of offset {msg.offset()} failed: {failures[0]}")
            if offset is None:
                # only after Kafka confirmed the send -- a crash before re-sends, never loses
                consumer.commit(msg, asynchronous=False)
            sent += 1
    finally:
        consumer.close()
    return sent


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dlq", help="e.g. payment-events.dlq")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--list", action="store_true")
    action.add_argument("--replay", action="store_true")
    parser.add_argument("--offset", type=int, help="with --replay: only this message")
    args = parser.parse_args()

    if args.list:
        for row in list_dead_letters(args.dlq):
            print(row)
        return
    if target_topic(args.dlq) == "booking-events":
        print(
            "note: booking-events reaches every group (Payment, Notification) -- their Inbox skips what's done"
        )
    print(f"replayed {replay(args.dlq, args.offset)} message(s) to {target_topic(args.dlq)}")


if __name__ == "__main__":
    main()
