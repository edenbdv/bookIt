"""BOOK-64 step 2b: the outbox event now carries everything Notification's
emails need (event-carried state transfer, microservices-decisions.md §5).

This payload is a contract with another service -- these tests pin its
shape, so a field can't be dropped or renamed without a test failing first.
"""

import json
import uuid
from datetime import UTC, date, datetime

import pytest

from app.auth.models import User
from app.core.outbox.models import OutboxEvent
from app.property.models import Property
from app.reservation.models import Reservation, ReservationStatus
from app.reservation.service import cancel_reservation, confirm_reservation

GUEST = User(id=uuid.uuid4(), email="dana@test.com", display_name="Dana", hashed_password="x")
PROPERTY = Property(id=uuid.uuid4(), title="Cabin", city="Tel Aviv", country="IL")


def _reservation(**overrides) -> Reservation:
    fields = {
        "id": uuid.uuid4(),
        "guest_id": GUEST.id,
        "property_id": PROPERTY.id,
        "check_in": date(2026, 9, 1),
        "check_out": date(2026, 9, 5),
        "guest_count": 2,
        "total_cents": 40000,
        "expires_at": datetime(2026, 8, 20, 10, 30, tzinfo=UTC),
        "status": ReservationStatus.PENDING,
    }
    return Reservation(**{**fields, **overrides})


class FakeSession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, obj: object) -> None:
        self.added.append(obj)


class _Repo:
    def __init__(self, obj: object) -> None:
        self._obj = obj

    async def get_by_id(self, id: uuid.UUID) -> object:
        return self._obj


class FakeUnitOfWork:
    def __init__(self, reservation: Reservation) -> None:
        self.reservations = _Repo(reservation)
        self.users = _Repo(GUEST)
        self.properties = _Repo(PROPERTY)
        self.session = FakeSession()


def _payload(uow: FakeUnitOfWork) -> dict:
    [event] = [obj for obj in uow.session.added if isinstance(obj, OutboxEvent)]
    return event.payload


@pytest.mark.anyio
async def test_confirmed_event_carries_the_email_snapshot() -> None:
    reservation = _reservation()
    uow = FakeUnitOfWork(reservation)

    await confirm_reservation(uow, reservation.id)

    payload = _payload(uow)
    # the original thin fields are still there -- additive, not replaced
    assert payload["event_type"] == "booking_confirmed"
    assert payload["reservation_id"] == str(reservation.id)
    assert "event_id" in payload and "occurred_at" in payload
    # and everything the email needs, so Notification never reads Booking's DB
    assert payload["guest"] == {"email": "dana@test.com", "display_name": "Dana"}
    assert payload["property"] == {"title": "Cabin", "city": "Tel Aviv", "country": "IL"}
    assert payload["reservation"] == {
        "check_in": "2026-09-01",
        "check_out": "2026-09-05",
        "guest_count": 2,
        "total_cents": 40000,
        "expires_at": "2026-08-20T10:30:00+00:00",
    }


@pytest.mark.anyio
async def test_cancelled_event_carries_the_same_snapshot() -> None:
    reservation = _reservation()
    uow = FakeUnitOfWork(reservation)

    await cancel_reservation(uow, reservation.id, GUEST.id)

    payload = _payload(uow)
    assert payload["event_type"] == "booking_cancelled"
    assert payload["guest"]["email"] == "dana@test.com"
    assert payload["property"]["title"] == "Cabin"
    assert payload["reservation"]["check_in"] == "2026-09-01"


# the payload goes into a JSONB column and then over Kafka as JSON -- a stray
# date/datetime/UUID object would fail at flush time, not here
@pytest.mark.anyio
async def test_payload_is_json_serializable() -> None:
    reservation = _reservation()
    uow = FakeUnitOfWork(reservation)

    await confirm_reservation(uow, reservation.id)

    json.dumps(_payload(uow))


@pytest.mark.anyio
async def test_missing_hold_expiry_is_null_not_an_error() -> None:
    reservation = _reservation(expires_at=None)
    uow = FakeUnitOfWork(reservation)

    await confirm_reservation(uow, reservation.id)

    assert _payload(uow)["reservation"]["expires_at"] is None
