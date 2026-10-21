"""What each email says -- recipient, subject and the key lines of the body.

Written in BOOK-64 step 1 to lock the emails before their data source moved
from Booking's tables to the event payload; every expected line below
survived that move unchanged. All three emails are now built from the
payload alone, so the tests just hand one in.

_send is patched, so nothing is actually sent over SMTP.
"""

import uuid
from unittest.mock import patch

import pytest
from notification import service

# one booking, in the shape reservation/service.py's _email_snapshot writes
# (ISO strings, not date objects) -- the cross-service contract
PAYLOAD = {
    "event_id": str(uuid.uuid4()),
    "event_type": "booking_confirmed",
    "reservation_id": str(uuid.uuid4()),
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


async def _sent_email(send_fn, payload: dict = PAYLOAD) -> dict:
    with patch.object(service, "_send") as send:
        await send_fn(payload)
    send.assert_called_once()
    return send.call_args.kwargs


@pytest.mark.anyio
async def test_received_email() -> None:
    email = await _sent_email(
        service.send_reservation_received_email, {**PAYLOAD, "event_type": "booking_created"}
    )

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
    nameless = {**PAYLOAD, "guest": {"email": "dana@test.com", "display_name": None}}

    email = await _sent_email(service.send_reservation_confirmed_email, nameless)

    assert "Hi there," in email["body"]
