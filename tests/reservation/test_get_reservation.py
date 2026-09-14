import uuid

import pytest

from app.property.models import Property
from app.reservation.models import Reservation
from app.reservation.service import (
    NotAParticipantError,
    ReservationNotFoundError,
    get_reservation,
)


class FakeReservationRepository:
    def __init__(self, reservation: Reservation | None) -> None:
        self._reservation = reservation

    async def get_by_id(self, id: uuid.UUID) -> Reservation | None:
        return self._reservation


class FakePropertyRepository:
    def __init__(self, property_: Property | None) -> None:
        self._property = property_

    async def get_by_id(self, id: uuid.UUID) -> Property | None:
        return self._property


class FakeUnitOfWork:
    def __init__(self, reservation: Reservation | None, property_: Property | None) -> None:
        self.reservations = FakeReservationRepository(reservation)
        self.properties = FakePropertyRepository(property_)


@pytest.mark.anyio
async def test_the_guest_can_fetch_their_own_reservation() -> None:
    guest_id = uuid.uuid4()
    property_ = Property(
        id=uuid.uuid4(), host_id=uuid.uuid4(), max_guests=2, price_per_night_cents=1000
    )
    reservation = Reservation(property_id=property_.id, guest_id=guest_id)
    uow = FakeUnitOfWork(reservation, property_)

    result = await get_reservation(uow, uuid.uuid4(), guest_id)

    assert result is reservation


@pytest.mark.anyio
async def test_the_property_host_can_fetch_it_too() -> None:
    host_id = uuid.uuid4()
    property_ = Property(id=uuid.uuid4(), host_id=host_id, max_guests=2, price_per_night_cents=1000)
    reservation = Reservation(property_id=property_.id, guest_id=uuid.uuid4())
    uow = FakeUnitOfWork(reservation, property_)

    result = await get_reservation(uow, uuid.uuid4(), host_id)

    assert result is reservation


@pytest.mark.anyio
async def test_an_unrelated_user_raises_not_a_participant() -> None:
    property_ = Property(
        id=uuid.uuid4(), host_id=uuid.uuid4(), max_guests=2, price_per_night_cents=1000
    )
    reservation = Reservation(property_id=property_.id, guest_id=uuid.uuid4())
    uow = FakeUnitOfWork(reservation, property_)

    with pytest.raises(NotAParticipantError):
        await get_reservation(uow, uuid.uuid4(), uuid.uuid4())


@pytest.mark.anyio
async def test_nonexistent_reservation_raises_not_found() -> None:
    uow = FakeUnitOfWork(None, None)

    with pytest.raises(ReservationNotFoundError):
        await get_reservation(uow, uuid.uuid4(), uuid.uuid4())
