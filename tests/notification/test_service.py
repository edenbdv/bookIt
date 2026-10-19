"""BOOK-64 step 1: lock what each email says before step 2 changes *where*
its data comes from (Booking's tables -> the event payload). After that
change these assertions must still pass: same recipient, same subject, same
key lines in the body.

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


async def _sent_email(send_fn) -> dict:
    with patch.object(service, "_send") as send:
        await send_fn(FakeUnitOfWork(), RESERVATION.id)
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
    email = await _sent_email(service.send_reservation_confirmed_email)

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
    email = await _sent_email(service.send_reservation_cancelled_email)

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
    nameless = User(id=GUEST.id, email=GUEST.email, display_name=None, hashed_password="x")
    uow = FakeUnitOfWork()
    uow.users = _Repo(nameless)

    with patch.object(service, "_send") as send:
        await service.send_reservation_confirmed_email(uow, RESERVATION.id)

    assert "Hi there," in send.call_args.kwargs["body"]
