import uuid
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.saga import reconcile_stale_payments


class FakePaymentRepository:
    def __init__(self, stale: list[Payment]) -> None:
        self._stale = stale
        self._by_id = {p.id: p for p in stale}

    async def list_stale_pending(self, older_than: datetime) -> list[Payment]:
        return self._stale

    async def get_by_id(self, id: uuid.UUID) -> Payment | None:
        return self._by_id.get(id)


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass


class FakeUnitOfWork:
    def __init__(self, stale: list[Payment]) -> None:
        self.payments = FakePaymentRepository(stale)
        self.session = FakeSession()


def _payment(
    kind: PaymentKind = PaymentKind.CHARGE, status: PaymentStatus = PaymentStatus.PENDING
) -> Payment:
    return Payment(
        id=uuid.uuid4(), reservation_id=uuid.uuid4(), kind=kind, amount_cents=10_000, status=status
    )


@pytest.mark.anyio
async def test_no_stale_payments_resolves_nothing() -> None:
    uow = FakeUnitOfWork([])

    count = await reconcile_stale_payments(uow, datetime.now(UTC))

    assert count == 0


@pytest.mark.anyio
async def test_each_stale_charge_is_checked_and_resolved() -> None:
    p1, p2 = _payment(), _payment()
    uow = FakeUnitOfWork([p1, p2])

    with patch(
        "app.payment.saga.provider_check_status",
        side_effect=[PaymentOutcome.SUCCESS, PaymentOutcome.FAILURE],
    ) as check_status:
        count = await reconcile_stale_payments(uow, datetime.now(UTC))

    assert count == 2
    assert p1.status == PaymentStatus.SUCCEEDED
    assert p2.status == PaymentStatus.FAILED
    assert check_status.call_count == 2
    assert len(uow.session.added) == 2  # one payment-events row each


@pytest.mark.anyio
async def test_a_stale_refund_resolves_without_reacting() -> None:
    # only a CHARGE writes an event -- a REFUND's outcome drives nothing
    refund = _payment(kind=PaymentKind.REFUND)
    uow = FakeUnitOfWork([refund])

    with patch("app.payment.saga.provider_check_status", return_value=PaymentOutcome.SUCCESS):
        count = await reconcile_stale_payments(uow, datetime.now(UTC))

    assert count == 1
    assert refund.status == PaymentStatus.SUCCEEDED
