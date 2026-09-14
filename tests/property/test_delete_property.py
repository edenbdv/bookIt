import uuid

import pytest

from app.property.models import Property
from app.property.service import (
    NotPropertyOwnerError,
    PropertyHasActiveReservationsError,
    delete_property,
)


# minimal fake standing in for a session -- delete_property never touches it
# directly (deleted_at is set on the Property object itself), but every
# FakeUnitOfWork in this test suite carries one for consistency with the
# real UnitOfWork's shape
class FakeSession:
    pass


class FakePropertyRepository:
    def __init__(self, property_: Property | None) -> None:
        self._property = property_

    async def get_active_by_id(self, id: uuid.UUID) -> Property | None:
        return self._property


class FakeReservationRepository:
    def __init__(self, *, has_active: bool) -> None:
        self._has_active = has_active

    async def has_active_by_property_id(self, property_id: uuid.UUID) -> bool:
        return self._has_active


class FakeUnitOfWork:
    def __init__(self, property_: Property | None, *, has_active_reservation: bool = False) -> None:
        self.properties = FakePropertyRepository(property_)
        self.reservations = FakeReservationRepository(has_active=has_active_reservation)
        self.session = FakeSession()


@pytest.mark.anyio
async def test_delete_by_owning_host_with_no_active_reservations_succeeds() -> None:
    host_id = uuid.uuid4()
    property_ = Property(host_id=host_id, max_guests=2, price_per_night_cents=1000)
    uow = FakeUnitOfWork(property_, has_active_reservation=False)

    result = await delete_property(uow, uuid.uuid4(), host_id)

    assert result is True
    assert property_.deleted_at is not None


@pytest.mark.anyio
async def test_delete_nonexistent_property_returns_false() -> None:
    uow = FakeUnitOfWork(None)

    result = await delete_property(uow, uuid.uuid4(), uuid.uuid4())

    assert result is False


@pytest.mark.anyio
async def test_delete_by_someone_else_raises_and_leaves_property_untouched() -> None:
    host_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    property_ = Property(host_id=host_id, max_guests=2, price_per_night_cents=1000)
    uow = FakeUnitOfWork(property_, has_active_reservation=False)

    with pytest.raises(NotPropertyOwnerError):
        await delete_property(uow, uuid.uuid4(), other_user_id)

    assert property_.deleted_at is None


@pytest.mark.anyio
async def test_delete_with_an_active_reservation_raises_and_leaves_property_untouched() -> None:
    # simulates: a guest holds a PENDING or CONFIRMED reservation on this
    # property -- see PropertyHasActiveReservationsError's own comment in
    # service.py for why this can't be allowed to silently delist anyway
    host_id = uuid.uuid4()
    property_ = Property(host_id=host_id, max_guests=2, price_per_night_cents=1000)
    uow = FakeUnitOfWork(property_, has_active_reservation=True)

    with pytest.raises(PropertyHasActiveReservationsError):
        await delete_property(uow, uuid.uuid4(), host_id)

    assert property_.deleted_at is None


@pytest.mark.anyio
async def test_ownership_is_checked_before_active_reservations() -> None:
    # same ordering as update_property: a 403 for a real property you don't
    # own takes priority over a 409 about its reservations, which is none of
    # a non-owner's business to learn about
    host_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    property_ = Property(host_id=host_id, max_guests=2, price_per_night_cents=1000)
    uow = FakeUnitOfWork(property_, has_active_reservation=True)

    with pytest.raises(NotPropertyOwnerError):
        await delete_property(uow, uuid.uuid4(), other_user_id)
