from datetime import UTC, date, datetime, timedelta

import pytest

from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import complete_past_reservations

YESTERDAY = datetime.now(UTC).date() - timedelta(days=1)


# same minimal-fake approach as test_expire_pending_reservations.py
class FakeReservationRepository:
    def __init__(self, reservations: list[Reservation]) -> None:
        self._reservations = reservations

    async def list_confirmed_past_checkout(self, today: date) -> list[Reservation]:
        return self._reservations


class FakeUnitOfWork:
    def __init__(self, reservations: list[Reservation]) -> None:
        self.reservations = FakeReservationRepository(reservations)


@pytest.mark.anyio
async def test_completes_every_candidate_returned_by_the_repository() -> None:
    past_due = Reservation(status=ReservationStatus.CONFIRMED, check_out=YESTERDAY)
    uow = FakeUnitOfWork([past_due])

    count = await complete_past_reservations(uow)

    assert past_due.status == ReservationStatus.COMPLETED
    assert count == 1


@pytest.mark.anyio
async def test_no_candidates_completes_nothing() -> None:
    uow = FakeUnitOfWork([])

    count = await complete_past_reservations(uow)

    assert count == 0
