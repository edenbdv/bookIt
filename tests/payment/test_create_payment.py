import uuid

import pytest

from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.service import (
    NotReservationGuestError,
    PendingChargeExistsError,
    ReservationNotFoundError,
    ReservationNotPayableError,
    create_payment,
)
from app.reservation.models import Reservation, ReservationStatus


# same minimal-fake approach as tests/reservation/*.py -- only what
# create_payment actually calls on uow is faked
class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass


class FakePaymentRepository:
    def __init__(self, pending_charge: Payment | None = None) -> None:
        self.added: list[Payment] = []
        self._pending_charge = pending_charge

    def add(self, payment: Payment) -> None:
        self.added.append(payment)

    async def get_pending_charge_by_reservation_id(
        self, reservation_id: uuid.UUID
    ) -> Payment | None:
        return self._pending_charge


class FakeReservationRepository:
    def __init__(self, reservation: Reservation | None) -> None:
        self._reservation = reservation

    async def get_by_id(self, id: uuid.UUID) -> Reservation | None:
        return self._reservation


class FakeUnitOfWork:
    def __init__(
        self, reservation: Reservation | None, pending_charge: Payment | None = None
    ) -> None:
        self.reservations = FakeReservationRepository(reservation)
        self.payments = FakePaymentRepository(pending_charge)
        self.session = FakeSession()


@pytest.mark.anyio
async def test_nonexistent_reservation_raises_not_found() -> None:
    uow = FakeUnitOfWork(None)

    with pytest.raises(ReservationNotFoundError):
        await create_payment(uow, uuid.uuid4(), uuid.uuid4())


@pytest.mark.anyio
async def test_paying_for_someone_elses_reservation_raises_not_guest() -> None:
    reservation = Reservation(
        guest_id=uuid.uuid4(), status=ReservationStatus.PENDING, total_cents=10_000
    )
    uow = FakeUnitOfWork(reservation)

    with pytest.raises(NotReservationGuestError):
        await create_payment(uow, uuid.uuid4(), uuid.uuid4())


@pytest.mark.anyio
async def test_paying_for_a_non_pending_reservation_raises_not_payable() -> None:
    guest_id = uuid.uuid4()
    reservation = Reservation(
        guest_id=guest_id, status=ReservationStatus.CONFIRMED, total_cents=10_000
    )
    uow = FakeUnitOfWork(reservation)

    with pytest.raises(ReservationNotPayableError):
        await create_payment(uow, uuid.uuid4(), guest_id)


@pytest.mark.anyio
async def test_success_outcome_marks_payment_succeeded() -> None:
    guest_id = uuid.uuid4()
    reservation = Reservation(
        guest_id=guest_id, status=ReservationStatus.PENDING, total_cents=10_000
    )
    uow = FakeUnitOfWork(reservation)

    payment = await create_payment(uow, uuid.uuid4(), guest_id, force=PaymentOutcome.SUCCESS)

    assert payment.status == PaymentStatus.SUCCEEDED
    assert payment.kind == PaymentKind.CHARGE
    assert payment.amount_cents == 10_000
    assert uow.payments.added == [payment]


@pytest.mark.anyio
async def test_failure_outcome_marks_payment_failed() -> None:
    guest_id = uuid.uuid4()
    reservation = Reservation(
        guest_id=guest_id, status=ReservationStatus.PENDING, total_cents=10_000
    )
    uow = FakeUnitOfWork(reservation)

    payment = await create_payment(uow, uuid.uuid4(), guest_id, force=PaymentOutcome.FAILURE)

    assert payment.status == PaymentStatus.FAILED


@pytest.mark.anyio
async def test_timeout_outcome_leaves_payment_pending() -> None:
    guest_id = uuid.uuid4()
    reservation = Reservation(
        guest_id=guest_id, status=ReservationStatus.PENDING, total_cents=10_000
    )
    uow = FakeUnitOfWork(reservation)

    payment = await create_payment(uow, uuid.uuid4(), guest_id, force=PaymentOutcome.TIMEOUT)

    assert payment.status == PaymentStatus.PENDING


@pytest.mark.anyio
async def test_retry_while_a_charge_is_still_pending_is_rejected() -> None:
    # simulates: an earlier attempt timed out and is still unresolved --
    # a second attempt must not start a second, independent charge
    guest_id = uuid.uuid4()
    reservation = Reservation(
        guest_id=guest_id, status=ReservationStatus.PENDING, total_cents=10_000
    )
    stuck_payment = Payment(
        reservation_id=uuid.uuid4(),
        kind=PaymentKind.CHARGE,
        amount_cents=10_000,
        status=PaymentStatus.PENDING,
    )
    uow = FakeUnitOfWork(reservation, pending_charge=stuck_payment)

    with pytest.raises(PendingChargeExistsError):
        await create_payment(uow, uuid.uuid4(), guest_id, force=PaymentOutcome.SUCCESS)
