import uuid
from unittest.mock import patch

import pytest

from app.core.outbox.models import OutboxEvent, OutboxEventType
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import (
    IllegalTransitionError,
    NotPropertyHostError,
    ReservationNotFoundError,
    host_cancel_reservation,
)


# BOOK-64: writing an event now also loads the guest and property for the
# email snapshot. That's not what this file tests (status transitions and
# which event is written), so it's stubbed here; the payload's contents
# have their own tests in test_event_payload.py.
@pytest.fixture(autouse=True)
def _no_email_snapshot():
    async def empty(uow, reservation):
        return {}

    with patch("app.reservation.service._email_snapshot", empty):
        yield


# same fake approach as test_cancel_reservation.py
class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


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
        self.session = FakeSession()


@pytest.mark.anyio
async def test_cancel_by_owning_host_succeeds() -> None:
    host_id = uuid.uuid4()
    property_ = Property(id=uuid.uuid4(), host_id=host_id, max_guests=2, price_per_night_cents=1000)
    reservation = Reservation(property_id=property_.id, status=ReservationStatus.CONFIRMED)
    uow = FakeUnitOfWork(reservation, property_)

    result = await host_cancel_reservation(uow, uuid.uuid4(), host_id)

    assert result.status == ReservationStatus.CANCELLED
    [outbox_event] = uow.session.added
    assert isinstance(outbox_event, OutboxEvent)
    assert outbox_event.event_type == OutboxEventType.BOOKING_CANCELLED


@pytest.mark.anyio
async def test_cancel_nonexistent_reservation_raises_not_found() -> None:
    uow = FakeUnitOfWork(None, None)

    with pytest.raises(ReservationNotFoundError):
        await host_cancel_reservation(uow, uuid.uuid4(), uuid.uuid4())


@pytest.mark.anyio
async def test_cancel_by_someone_else_raises_and_leaves_status_unchanged() -> None:
    host_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    property_ = Property(id=uuid.uuid4(), host_id=host_id, max_guests=2, price_per_night_cents=1000)
    reservation = Reservation(property_id=property_.id, status=ReservationStatus.CONFIRMED)
    uow = FakeUnitOfWork(reservation, property_)

    with pytest.raises(NotPropertyHostError):
        await host_cancel_reservation(uow, uuid.uuid4(), other_user_id)

    assert reservation.status == ReservationStatus.CONFIRMED
    assert uow.session.added == []


@pytest.mark.anyio
async def test_the_guest_who_booked_it_cannot_use_this_endpoint() -> None:
    # this checks property.host_id, not reservation.guest_id
    host_id = uuid.uuid4()
    guest_id = uuid.uuid4()
    property_ = Property(id=uuid.uuid4(), host_id=host_id, max_guests=2, price_per_night_cents=1000)
    reservation = Reservation(
        property_id=property_.id, guest_id=guest_id, status=ReservationStatus.CONFIRMED
    )
    uow = FakeUnitOfWork(reservation, property_)

    with pytest.raises(NotPropertyHostError):
        await host_cancel_reservation(uow, uuid.uuid4(), guest_id)


@pytest.mark.anyio
async def test_cancel_already_cancelled_reservation_raises_illegal_transition() -> None:
    host_id = uuid.uuid4()
    property_ = Property(id=uuid.uuid4(), host_id=host_id, max_guests=2, price_per_night_cents=1000)
    reservation = Reservation(property_id=property_.id, status=ReservationStatus.CANCELLED)
    uow = FakeUnitOfWork(reservation, property_)

    with pytest.raises(IllegalTransitionError):
        await host_cancel_reservation(uow, uuid.uuid4(), host_id)
