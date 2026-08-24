import uuid

import pytest

from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import (
    IllegalTransitionError,
    NotReservationGuestError,
    ReservationNotFoundError,
    cancel_reservation,
)


# minimal fake standing in for UnitOfWork -- cancel_reservation only ever
# calls uow.reservations.get_by_id, so that's the only thing faked
# (same "call service.foo(fake_uow, ...) directly, no DB" approach
# docs/domain-layer/router-service-basics.md describes)
class FakeReservationRepository:
    def __init__(self, reservation: Reservation | None) -> None:
        self._reservation = reservation

    async def get_by_id(self, id: uuid.UUID) -> Reservation | None:
        return self._reservation


class FakeUnitOfWork:
    def __init__(self, reservation: Reservation | None) -> None:
        self.reservations = FakeReservationRepository(reservation)


@pytest.mark.anyio
async def test_cancel_by_owning_guest_succeeds() -> None:
    guest_id = uuid.uuid4()
    reservation = Reservation(guest_id=guest_id, status=ReservationStatus.PENDING)
    uow = FakeUnitOfWork(reservation)

    result = await cancel_reservation(uow, uuid.uuid4(), guest_id)

    assert result.status == ReservationStatus.CANCELLED


@pytest.mark.anyio
async def test_cancel_nonexistent_reservation_raises_not_found() -> None:
    uow = FakeUnitOfWork(None)

    with pytest.raises(ReservationNotFoundError):
        await cancel_reservation(uow, uuid.uuid4(), uuid.uuid4())


@pytest.mark.anyio
async def test_cancel_by_someone_else_raises_and_leaves_status_unchanged() -> None:
    guest_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    reservation = Reservation(guest_id=guest_id, status=ReservationStatus.PENDING)
    uow = FakeUnitOfWork(reservation)

    with pytest.raises(NotReservationGuestError):
        await cancel_reservation(uow, uuid.uuid4(), other_user_id)

    assert reservation.status == ReservationStatus.PENDING


@pytest.mark.anyio
async def test_cancel_already_cancelled_reservation_raises_illegal_transition() -> None:
    guest_id = uuid.uuid4()
    reservation = Reservation(guest_id=guest_id, status=ReservationStatus.CANCELLED)
    uow = FakeUnitOfWork(reservation)

    with pytest.raises(IllegalTransitionError):
        await cancel_reservation(uow, uuid.uuid4(), guest_id)
