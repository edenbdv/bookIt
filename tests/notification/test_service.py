"""BOOK-64 step 1: lock what each email says before step 2 changes *where*
its data comes from (Booking's tables -> the event payload). After that
change these assertions must still pass: same recipient, same subject, same
key lines in the body. Step 2c done for confirmed/cancelled -- they now take
the payload, and every expected line is unchanged.

_send is patched, so nothing is actually sent over SMTP.
"""

import uuid
from datetime import UTC, date, datetime
from unittest.mock import patch

import pytest

from app.auth.models import User
from app.notification import service
from app.property.models import Property
from app.reservation.models import Reservation

GUEST = User(id=uuid.uuid4(), email="dana@test.com", display_name="Dana", hashed_password="x")
PROPERTY = Property(id=uuid.uuid4(), title="Cabin", city="Tel Aviv", country="IL")
RESERVATION = Reservation(
    id=uuid.uuid4(),
    guest_id=GUEST.id,
    property_id=PROPERTY.id,
    check_in=date(2026, 9, 1),
    check_out=date(2026, 9, 5),
    guest_count=2,
    total_cents=40000,
    expires_at=datetime(2026, 8, 20, 10, 30, 15, 123456, tzinfo=UTC),
)


class _Repo:
    def __init__(self, obj: object) -> None:
        self._obj = obj

    async def get_by_id(self, id: uuid.UUID) -> object:
        return self._obj


# the email functions only read through these three repos
class FakeUnitOfWork:
    def __init__(self) -> None:
        self.reservations = _Repo(RESERVATION)
        self.users = _Repo(GUEST)
        self.properties = _Repo(PROPERTY)


# BOOK-64 step 2c: confirmed/cancelled are built from the event payload, not
# the DB -- the same booking as RESERVATION/GUEST/PROPERTY above, in the
# shape reservation/service.py's _email_snapshot writes (ISO strings, not
# date objects). The expected email lines below did NOT change: that's the
# proof the switch from DB to payload left the emails identical.
PAYLOAD = {
    "event_id": str(uuid.uuid4()),
    "event_type": "booking_confirmed",
    "reservation_id": str(RESERVATION.id),
    "occurred_at": "2026-08-19T09:00:00+00:00",
    "guest": {"email": "dana@test.com", "display_name": "Dana"},
    "property": {"title": "Cabin", "city": "Tel Aviv", "country": "IL"},
    "reservation": {
        "check_in": "2026-09-01",
        "check_out": "2026-09-05",
        "guest_count": 2,
        "total_cents": 40000,
        "expires_at": "2026-08-20T10:30:15.123456+00:00",
    },
}


async def _sent_email(send_fn) -> dict:
    # "received" still reads Booking's tables until step 3 moves it to Kafka
    with patch.object(service, "_send") as send:
        await send_fn(FakeUnitOfWork(), RESERVATION.id)
    send.assert_called_once()
    return send.call_args.kwargs


async def _sent_email_from_payload(send_fn, payload: dict = PAYLOAD) -> dict:
    with patch.object(service, "_send") as send:
        await send_fn(payload)
    send.assert_called_once()
    return send.call_args.kwargs


@pytest.mark.anyio
async def test_received_email() -> None:
    email = await _sent_email(service.send_reservation_received_email)

    assert email["to"] == "dana@test.com"
    assert email["subject"] == "Your reservation request for Cabin"
    for line in (
        "Hi Dana,",
        "Property: Cabin, Tel Aviv, IL",
        "Dates: 2026-09-01 -> 2026-09-05",
        "Guests: 2",
        "Total: $400.00",
        # microseconds and +00:00 dropped -- _format_datetime
        "held until 2026-08-20 10:30 UTC",
    ):
        assert line in email["body"]


@pytest.mark.anyio
async def test_confirmed_email() -> None:
    email = await _sent_email_from_payload(service.send_reservation_confirmed_email)

    assert email["to"] == "dana@test.com"
    assert email["subject"] == "Your reservation for Cabin is confirmed"
    for line in (
        "Hi Dana,",
        "your reservation is confirmed",
        "Property: Cabin, Tel Aviv, IL",
        "Dates: 2026-09-01 -> 2026-09-05",
        "Guests: 2",
        "Total: $400.00",
    ):
        assert line in email["body"]


@pytest.mark.anyio
async def test_cancelled_email() -> None:
    email = await _sent_email_from_payload(service.send_reservation_cancelled_email)

    assert email["to"] == "dana@test.com"
    assert email["subject"] == "Your reservation for Cabin has been cancelled"
    for line in (
        "Hi Dana,",
        "Property: Cabin, Tel Aviv, IL",
        "Dates: 2026-09-01 -> 2026-09-05",
        "available again",
    ):
        assert line in email["body"]
    # neutral wording -- fires for an explicit cancel *and* a silent expiry
    assert "you cancelled" not in email["body"].lower()


# a guest with no display_name still gets a sensible greeting
@pytest.mark.anyio
async def test_greeting_falls_back_without_a_display_name() -> None:
    nameless = {**PAYLOAD, "guest": {"email": "dana@test.com", "display_name": None}}

    email = await _sent_email_from_payload(service.send_reservation_confirmed_email, nameless)

    assert "Hi there," in email["body"]
