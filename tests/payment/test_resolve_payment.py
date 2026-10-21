import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.saga import resolve_payment_and_react
from app.payment.service import PaymentNotFoundError


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
        await resolve_payment_and_react(uow, uuid.uuid4(), PaymentOutcome.SUCCESS)


@pytest.mark.anyio
async def test_already_resolved_payment_is_a_noop() -> None:
    payment = _payment(PaymentKind.CHARGE, PaymentStatus.SUCCEEDED)
    uow = FakeUnitOfWork(payment)

    with patch("app.payment.saga.react_to_payment_outcome", new_callable=AsyncMock) as react:
        result = await resolve_payment_and_react(uow, payment.id, PaymentOutcome.FAILURE)

    assert result is payment
    assert payment.status == PaymentStatus.SUCCEEDED  # untouched -- redelivery, no-op
    react.assert_not_called()


@pytest.mark.anyio
async def test_pending_charge_resolves_and_reacts() -> None:
    payment = _payment(PaymentKind.CHARGE, PaymentStatus.PENDING)
    uow = FakeUnitOfWork(payment)

    with patch("app.payment.saga.react_to_payment_outcome", new_callable=AsyncMock) as react:
        await resolve_payment_and_react(uow, payment.id, PaymentOutcome.SUCCESS)

    assert payment.status == PaymentStatus.SUCCEEDED
    react.assert_awaited_once_with(uow, payment)


@pytest.mark.anyio
async def test_pending_refund_resolves_without_reacting() -> None:
    payment = _payment(PaymentKind.REFUND, PaymentStatus.PENDING)
    uow = FakeUnitOfWork(payment)

    with patch("app.payment.saga.react_to_payment_outcome", new_callable=AsyncMock) as react:
        await resolve_payment_and_react(uow, payment.id, PaymentOutcome.FAILURE)

    assert payment.status == PaymentStatus.FAILED
    react.assert_not_called()
