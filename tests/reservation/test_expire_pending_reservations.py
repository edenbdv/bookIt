from datetime import UTC, datetime, timedelta

import pytest

from app.core.outbox.models import OutboxEvent, OutboxEventType
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import expire_pending_reservations

PAST = datetime.now(UTC) - timedelta(minutes=1)


# same minimal-fake approach as test_cancel_reservation.py -- only
# list_expired_pending and session.add are exercised here, so that's all
# that's faked
class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


class FakeReservationRepository:
    def __init__(self, reservations: list[Reservation]) -> None:
        self._reservations = reservations

    async def list_expired_pending(self, now: datetime) -> list[Reservation]:
        return self._reservations


class FakeUnitOfWork:
    def __init__(self, reservations: list[Reservation]) -> None:
        self.reservations = FakeReservationRepository(reservations)
        self.session = FakeSession()


@pytest.mark.anyio
async def test_expires_every_candidate_returned_by_the_repository() -> None:
    expired = Reservation(status=ReservationStatus.PENDING, expires_at=PAST)
    uow = FakeUnitOfWork([expired])

    count = await expire_pending_reservations(uow)

    assert expired.status == ReservationStatus.CANCELLED
    assert count == 1
    [outbox_event] = uow.session.added
    assert isinstance(outbox_event, OutboxEvent)
    assert outbox_event.event_type == OutboxEventType.BOOKING_CANCELLED
    assert outbox_event.reservation_id == expired.id


@pytest.mark.anyio
async def test_no_candidates_cancels_nothing() -> None:
    uow = FakeUnitOfWork([])

    count = await expire_pending_reservations(uow)

    assert count == 0
    assert uow.session.added == []
