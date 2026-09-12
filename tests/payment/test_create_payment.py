import uuid

import pytest

from app.payment.models import Payment, PaymentKind, PaymentStatus
from app.payment.provider import PaymentOutcome
from app.payment.service import (
    NotReservationGuestError,
    ReservationNotFoundError,
    ReservationNotPayableError,
    create_payment,
)
from app.reservation.models import Reservation, ReservationStatus


# same minimal-fake approach as tests/reservation/*.py -- create_payment only
# ever calls uow.reservations.get_by_id, uow.payments.add, and
# uow.session.flush(), so that's all that's faked
class FakeSession:
    async def flush(self) -> None:
        pass


class FakePaymentRepository:
    def __init__(self) -> None:
        self.added: list[Payment] = []

    def add(self, payment: Payment) -> None:
        self.added.append(payment)


class FakeReservationRepository:
    def __init__(self, reservation: Reservation | None) -> None:
        self._reservation = reservation

    async def get_by_id(self, id: uuid.UUID) -> Reservation | None:
        return self._reservation


class FakeUnitOfWork:
    def __init__(self, reservation: Reservation | None) -> None:
        self.reservations = FakeReservationRepository(reservation)
        self.payments = FakePaymentRepository()
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
