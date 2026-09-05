from datetime import UTC, datetime, timedelta

import pytest

from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import expire_pending_reservations

PAST = datetime.now(UTC) - timedelta(minutes=1)


# same minimal-fake approach as test_cancel_reservation.py -- only
# list_expired_pending is exercised here, so that's the only thing faked
class FakeReservationRepository:
    def __init__(self, reservations: list[Reservation]) -> None:
        self._reservations = reservations

    async def list_expired_pending(self, now: datetime) -> list[Reservation]:
        return self._reservations


class FakeUnitOfWork:
    def __init__(self, reservations: list[Reservation]) -> None:
        self.reservations = FakeReservationRepository(reservations)


@pytest.mark.anyio
async def test_expires_every_candidate_returned_by_the_repository() -> None:
    expired = Reservation(status=ReservationStatus.PENDING, expires_at=PAST)
    uow = FakeUnitOfWork([expired])

    count = await expire_pending_reservations(uow)

    assert expired.status == ReservationStatus.CANCELLED
    assert count == 1


@pytest.mark.anyio
async def test_no_candidates_cancels_nothing() -> None:
    uow = FakeUnitOfWork([])

    count = await expire_pending_reservations(uow)

    assert count == 0
