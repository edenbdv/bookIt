"""BOOK-64 step 1: lock the Kafka consumer's behavior before Notification is
extracted into its own service -- the Inbox dedup (worker-decisions.md §13)
and the commit-only-after-handling rule (§12). These must still hold,
unchanged, once this code lives in services/notification/.
"""

import json
import uuid
from typing import Self
from unittest.mock import patch

import pytest

from app.notification import consumer
from app.notification.models import ProcessedNotificationEvent


# minimal fakes -- dispatch() only ever calls session.get (the "already
# processed?" check) and session.add (marking it processed)
class FakeSession:
    def __init__(self, processed: set[uuid.UUID]) -> None:
        self._processed = processed
        self.added: list[object] = []

    async def get(self, model: type, key: uuid.UUID) -> object | None:
        return ProcessedNotificationEvent(event_id=key) if key in self._processed else None

    def add(self, obj: object) -> None:
        self.added.append(obj)


class FakeUnitOfWork:
    def __init__(self, processed: set[uuid.UUID]) -> None:
        self.session = FakeSession(processed)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        pass


def _payload(event_type: str = "booking_confirmed") -> dict:
    return {
        "event_id": str(uuid.uuid4()),
        "event_type": event_type,
        "reservation_id": str(uuid.uuid4()),
    }


@pytest.fixture
def handled() -> list[tuple[str, uuid.UUID]]:
    return []


@pytest.fixture
def handlers(handled: list[tuple[str, uuid.UUID]]) -> dict:
    def make(name: str):
        async def handler(uow: object, reservation_id: uuid.UUID) -> None:
            handled.append((name, reservation_id))

        return handler

    return {"booking_confirmed": make("confirmed"), "booking_cancelled": make("cancelled")}


@pytest.mark.anyio
async def test_new_event_is_handled_and_marked_processed(handlers, handled) -> None:
    uow = FakeUnitOfWork(processed=set())
    payload = _payload("booking_confirmed")

    with patch.object(consumer, "UnitOfWork", return_value=uow):
        await consumer.make_dispatcher(handlers)(payload)

    assert handled == [("confirmed", uuid.UUID(payload["reservation_id"]))]
    [marker] = uow.session.added
    assert isinstance(marker, ProcessedNotificationEvent)
    assert marker.event_id == uuid.UUID(payload["event_id"])


# the Inbox pattern itself: a redelivered event (same event_id) sends nothing
@pytest.mark.anyio
async def test_already_processed_event_is_skipped(handlers, handled) -> None:
    payload = _payload()
    uow = FakeUnitOfWork(processed={uuid.UUID(payload["event_id"])})

    with patch.object(consumer, "UnitOfWork", return_value=uow):
        await consumer.make_dispatcher(handlers)(payload)

    assert handled == []
    assert uow.session.added == []


@pytest.mark.anyio
async def test_event_type_picks_the_matching_handler(handlers, handled) -> None:
    uow = FakeUnitOfWork(processed=set())
    payload = _payload("booking_cancelled")

    with patch.object(consumer, "UnitOfWork", return_value=uow):
        await consumer.make_dispatcher(handlers)(payload)

    assert [name for name, _ in handled] == ["cancelled"]


# mark-after-send: if sending fails, the event is NOT marked processed -- so a
# redelivery retries it instead of the email being silently lost
@pytest.mark.anyio
async def test_failed_handler_does_not_mark_processed(handlers) -> None:
    async def failing(uow: object, reservation_id: uuid.UUID) -> None:
        raise RuntimeError("SMTP down")

    uow = FakeUnitOfWork(processed=set())
    with (
        patch.object(consumer, "UnitOfWork", return_value=uow),
        pytest.raises(RuntimeError),
    ):
        await consumer.make_dispatcher({**handlers, "booking_confirmed": failing})(
            _payload("booking_confirmed")
        )

    assert uow.session.added == []


# --- _process_one: the Kafka offset is committed only after handling ---


class FakeMessage:
    def __init__(self, payload: dict | None = None, error: str | None = None) -> None:
        self._value = json.dumps(payload).encode() if payload else b""
        self._error = error

    def value(self) -> bytes:
        return self._value

    def error(self) -> str | None:
        return self._error


class FakeConsumer:
    def __init__(self, message: FakeMessage | None) -> None:
        self._message = message
        self.committed: list[FakeMessage] = []

    def poll(self, timeout: float) -> FakeMessage | None:
        return self._message

    def commit(self, message: FakeMessage) -> None:
        self.committed.append(message)


@pytest.mark.anyio
async def test_offset_committed_after_successful_handling() -> None:
    message = FakeMessage(_payload())
    kafka = FakeConsumer(message)
    seen: list[dict] = []

    async def handle(payload: dict) -> None:
        seen.append(payload)

    await consumer._process_one(kafka, handle)

    assert len(seen) == 1
    assert kafka.committed == [message]


# at-least-once: a crash while handling leaves the offset uncommitted, so
# Kafka redelivers the message (and the Inbox catches it if it did send)
@pytest.mark.anyio
async def test_offset_not_committed_when_handling_fails() -> None:
    kafka = FakeConsumer(FakeMessage(_payload()))

    async def handle(payload: dict) -> None:
        raise RuntimeError("crash mid-handling")

    with pytest.raises(RuntimeError):
        await consumer._process_one(kafka, handle)

    assert kafka.committed == []


@pytest.mark.anyio
async def test_empty_poll_and_error_messages_are_ignored() -> None:
    async def handle(payload: dict) -> None:
        raise AssertionError("must not be called")

    for message in (None, FakeMessage(error="broker hiccup")):
        kafka = FakeConsumer(message)
        await consumer._process_one(kafka, handle)
        assert kafka.committed == []
