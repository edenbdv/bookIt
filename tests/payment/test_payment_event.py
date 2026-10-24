"""BOOK-65: emit_payment_event() -- which payments become a payment-events
outbox row, and what the payload carries. The payload is a contract with
Booking's consumer (additive only), so its fields are checked one by one."""

import uuid

import pytest

from app.payment.models import (
    Payment,
    PaymentEventType,
    PaymentKind,
    PaymentOutboxEvent,
    PaymentStatus,
)
from app.payment.service import emit_payment_event


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.session = FakeSession()


def _payment(kind: PaymentKind, status: PaymentStatus) -> Payment:
    return Payment(
        id=uuid.uuid4(),
        reservation_id=uuid.uuid4(),
        kind=kind,
        amount_cents=10_000,
        status=status,
    )


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("status", "event_type"),
    [
        (PaymentStatus.SUCCEEDED, PaymentEventType.PAYMENT_SUCCEEDED),
        (PaymentStatus.FAILED, PaymentEventType.PAYMENT_FAILED),
    ],
)
async def test_a_resolved_charge_writes_one_event(
    status: PaymentStatus, event_type: PaymentEventType
) -> None:
    payment = _payment(PaymentKind.CHARGE, status)
    uow = FakeUnitOfWork()

    await emit_payment_event(uow, payment)

    [event] = uow.session.added
    assert isinstance(event, PaymentOutboxEvent)
    assert event.event_type == event_type
    assert event.reservation_id == payment.reservation_id  # also the Kafka key
    assert event.payload["event_id"] == str(event.id)  # same id in row and payload (Inbox dedup)
    assert event.payload["event_type"] == event_type.value
    assert event.payload["reservation_id"] == str(payment.reservation_id)
    assert event.payload["payment_id"] == str(payment.id)
    assert event.payload["amount_cents"] == 10_000
    assert "occurred_at" in event.payload


@pytest.mark.anyio
async def test_a_pending_charge_writes_nothing() -> None:
    # a TIMEOUT -- no outcome yet; the event goes out once it resolves
    uow = FakeUnitOfWork()

    await emit_payment_event(uow, _payment(PaymentKind.CHARGE, PaymentStatus.PENDING))

    assert uow.session.added == []


@pytest.mark.anyio
@pytest.mark.parametrize("status", list(PaymentStatus))
async def test_a_refund_never_writes_an_event(status: PaymentStatus) -> None:
    uow = FakeUnitOfWork()

    await emit_payment_event(uow, _payment(PaymentKind.REFUND, status))

    assert uow.session.added == []
