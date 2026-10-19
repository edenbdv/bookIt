"""BOOK-64 step 1: lock the Kafka consumer's behavior before Notification is
extracted into its own service -- the Inbox dedup (worker-decisions.md §13)
and the commit-only-after-handling rule (§12). These must still hold,
unchanged, once this code lives in services/notification/.
"""

import json
import smtplib
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
def handled() -> list[tuple[str, dict]]:
    return []


@pytest.fixture
def handlers(handled: list[tuple[str, dict]]) -> dict:
    def make(name: str):
        async def handler(payload: dict) -> None:
            handled.append((name, payload))

        return handler

    return {"booking_confirmed": make("confirmed"), "booking_cancelled": make("cancelled")}


@pytest.mark.anyio
async def test_new_event_is_handled_and_marked_processed(handlers, handled) -> None:
    uow = FakeUnitOfWork(processed=set())
    payload = _payload("booking_confirmed")

    with patch.object(consumer, "UnitOfWork", return_value=uow):
        await consumer.make_dispatcher(handlers)(payload)

    # BOOK-64: the handler gets the whole event payload -- everything the
    # email needs travels in it
    assert handled == [("confirmed", payload)]
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


# BOOK-64: an event type Notification has no email for is skipped, not a
# KeyError -- dispatch returns normally, so _process_one commits the offset
# and the queue keeps moving instead of being stuck on a poison message
@pytest.mark.anyio
async def test_unknown_event_type_is_skipped_not_a_crash(handlers, handled) -> None:
    uow = FakeUnitOfWork(processed=set())

    # the logger itself is patched, not read through caplog: alembic's
    # fileConfig (run by the integration suite) disables existing loggers,
    # so caplog sees nothing when this runs after them
    with (
        patch.object(consumer, "UnitOfWork", return_value=uow),
        patch.object(consumer, "logger") as logger,
    ):
        await consumer.make_dispatcher(handlers)(_payload("booking_reviewed"))

    assert handled == []
    assert uow.session.added == []
    logger.warning.assert_called_once()
    assert "booking_reviewed" in logger.warning.call_args.args


# mark-after-send: if sending fails, the event is NOT marked processed -- so a
# redelivery retries it instead of the email being silently lost
@pytest.mark.anyio
async def test_failed_handler_does_not_mark_processed(handlers) -> None:
    async def failing(payload: dict) -> None:
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


# --- BOOK-64: retry with backoff, the policy the Celery email task used to have ---


@pytest.fixture
def no_retry_wait():
    # same number of retries, zero wait -- fast, and keeps the policy itself under test
    with patch.object(consumer, "RETRY_DELAYS", (0.0,) * len(consumer.RETRY_DELAYS)):
        yield


def _flaky(failures: list[Exception]):
    """A handler that raises each of `failures` in turn, then succeeds."""
    calls: list[dict] = []

    async def handler(payload: dict) -> None:
        calls.append(payload)
        if len(calls) <= len(failures):
            raise failures[len(calls) - 1]

    return handler, calls


@pytest.mark.anyio
async def test_transient_error_is_retried_then_marked_processed(no_retry_wait) -> None:
    handler, calls = _flaky([smtplib.SMTPServerDisconnected("blip"), OSError("conn reset")])
    uow = FakeUnitOfWork(processed=set())

    with patch.object(consumer, "UnitOfWork", return_value=uow):
        await consumer.make_dispatcher({"booking_confirmed": handler})(
            _payload("booking_confirmed")
        )

    assert len(calls) == 3  # two failures, then the send that worked
    [marker] = uow.session.added  # marked once, like any successful send
    assert isinstance(marker, ProcessedNotificationEvent)


# a refused address won't fix itself -- no retries, straight back to the caller
@pytest.mark.anyio
async def test_permanent_error_is_not_retried(no_retry_wait) -> None:
    refused = smtplib.SMTPRecipientsRefused({"dana@test.com": (550, b"no such user")})
    handler, calls = _flaky([refused])
    uow = FakeUnitOfWork(processed=set())

    with (
        patch.object(consumer, "UnitOfWork", return_value=uow),
        pytest.raises(smtplib.SMTPRecipientsRefused),
    ):
        await consumer.make_dispatcher({"booking_confirmed": handler})(
            _payload("booking_confirmed")
        )

    assert len(calls) == 1
    assert uow.session.added == []


@pytest.mark.anyio
async def test_transient_error_outlasting_every_retry_is_raised(no_retry_wait) -> None:
    attempts = len(consumer.RETRY_DELAYS) + 1
    handler, calls = _flaky([OSError("SMTP down")] * attempts)
    uow = FakeUnitOfWork(processed=set())

    with (
        patch.object(consumer, "UnitOfWork", return_value=uow),
        pytest.raises(OSError),
    ):
        await consumer.make_dispatcher({"booking_confirmed": handler})(
            _payload("booking_confirmed")
        )

    assert len(calls) == attempts  # the first try plus every retry
    assert uow.session.added == []  # never marked -- the email didn't go out


@pytest.mark.anyio
async def test_retries_wait_with_growing_delays() -> None:
    handler, _ = _flaky([OSError("down")] * 2)
    waited: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    with patch.object(consumer.asyncio, "sleep", fake_sleep):
        await consumer._with_retry(handler, _payload())

    assert waited == list(consumer.RETRY_DELAYS[:2])
    assert waited == sorted(waited)


# --- _process_one: the Kafka offset is committed only after handling ---


class FakeMessage:
    def __init__(
        self, payload: dict | None = None, error: str | None = None, raw: bytes | None = None
    ) -> None:
        self._value = raw if raw is not None else (json.dumps(payload).encode() if payload else b"")
        self._error = error

    def value(self) -> bytes:
        return self._value

    def error(self) -> str | None:
        return self._error

    def offset(self) -> int:
        return 42


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


# --- BOOK-64: dead-letter topic -- a message that fails for good is set aside ---


class FakeDeadLetter:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[bytes, Exception]] = []
        self._fail = fail

    async def __call__(self, value: bytes, error: Exception) -> None:
        if self._fail:
            raise RuntimeError("Kafka down too")
        self.sent.append((value, error))


@pytest.mark.anyio
async def test_failed_message_is_dead_lettered_and_committed() -> None:
    message = FakeMessage(_payload())
    kafka = FakeConsumer(message)
    dead_letter = FakeDeadLetter()

    async def handle(payload: dict) -> None:
        raise smtplib.SMTPRecipientsRefused({"x@test.com": (550, b"no such user")})

    await consumer._process_one(kafka, handle, dead_letter)  # must not raise

    [(value, error)] = dead_letter.sent
    assert value == message.value()  # the original bytes, untouched
    assert isinstance(error, smtplib.SMTPRecipientsRefused)
    # committed -- the queue moves on instead of being stuck on this message
    assert kafka.committed == [message]


# not even valid JSON -- can never succeed, so straight to the dead-letter topic
@pytest.mark.anyio
async def test_malformed_message_is_dead_lettered() -> None:
    message = FakeMessage(raw=b"not json {")
    kafka = FakeConsumer(message)
    dead_letter = FakeDeadLetter()

    async def handle(payload: dict) -> None:
        raise AssertionError("must not be reached")

    await consumer._process_one(kafka, handle, dead_letter)

    assert [value for value, _ in dead_letter.sent] == [b"not json {"]
    assert kafka.committed == [message]


# the one thing never allowed: losing a message. If it can't even be set
# aside, it stays uncommitted and is redelivered after the restart
@pytest.mark.anyio
async def test_dead_letter_failure_leaves_the_message_uncommitted() -> None:
    kafka = FakeConsumer(FakeMessage(_payload()))

    async def handle(payload: dict) -> None:
        raise RuntimeError("handling failed")

    with pytest.raises(RuntimeError, match="Kafka down too"):
        await consumer._process_one(kafka, handle, FakeDeadLetter(fail=True))

    assert kafka.committed == []


@pytest.mark.anyio
async def test_successful_message_never_touches_the_dead_letter_topic() -> None:
    message = FakeMessage(_payload())
    kafka = FakeConsumer(message)
    dead_letter = FakeDeadLetter()

    async def handle(payload: dict) -> None:
        pass

    await consumer._process_one(kafka, handle, dead_letter)

    assert dead_letter.sent == []
    assert kafka.committed == [message]
