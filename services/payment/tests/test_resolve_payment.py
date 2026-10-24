import uuid

import pytest
from payment.models import (
    Payment,
    PaymentEventType,
    PaymentKind,
    PaymentOutboxEvent,
    PaymentStatus,
)
from payment.provider import PaymentOutcome
from payment.service import PaymentNotFoundError, resolve_payment


class FakePaymentRepository:
    def __init__(self, payment: Payment | None) -> None:
        self._payment = payment

    async def get_by_id(self, id: uuid.UUID) -> Payment | None:
        return self._payment


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass


class FakeUnitOfWork:
    def __init__(self, payment: Payment | None) -> None:
        self.payments = FakePaymentRepository(payment)
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
async def test_unknown_payment_raises_not_found() -> None:
    uow = FakeUnitOfWork(None)

    with pytest.raises(PaymentNotFoundError):
        await resolve_payment(uow, uuid.uuid4(), PaymentOutcome.SUCCESS)


@pytest.mark.anyio
async def test_already_resolved_payment_is_a_noop() -> None:
    payment = _payment(PaymentKind.CHARGE, PaymentStatus.SUCCEEDED)
    uow = FakeUnitOfWork(payment)

    result = await resolve_payment(uow, payment.id, PaymentOutcome.FAILURE)

    assert result is payment
    assert payment.status == PaymentStatus.SUCCEEDED  # untouched -- redelivery, no-op
    assert uow.session.added == []  # and no second event


# BOOK-65: resolving no longer confirms anything itself -- it writes the
# payment-events row, and Booking confirms from that
@pytest.mark.anyio
async def test_pending_charge_resolves_and_writes_its_event() -> None:
    payment = _payment(PaymentKind.CHARGE, PaymentStatus.PENDING)
    uow = FakeUnitOfWork(payment)

    await resolve_payment(uow, payment.id, PaymentOutcome.SUCCESS)

    assert payment.status == PaymentStatus.SUCCEEDED
    [event] = uow.session.added
    assert isinstance(event, PaymentOutboxEvent)
    assert event.event_type == PaymentEventType.PAYMENT_SUCCEEDED


@pytest.mark.anyio
async def test_pending_refund_resolves_without_an_event() -> None:
    payment = _payment(PaymentKind.REFUND, PaymentStatus.PENDING)
    uow = FakeUnitOfWork(payment)

    await resolve_payment(uow, payment.id, PaymentOutcome.FAILURE)

    assert payment.status == PaymentStatus.FAILED
    assert uow.session.added == []
