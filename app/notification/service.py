"""Compose and send BookIt's transactional emails.

Deliberately the only thing this module knows how to do -- what *triggers*
a call here is someone else's job. Since BOOK-64 all three are called by the
Kafka consumer, one per booking event, and each is built from the event
payload alone -- no reads of Booking's tables (microservices-decisions.md §4-5).
"""

import asyncio
import smtplib
from datetime import datetime
from email.message import EmailMessage

from app.core.config import get_settings

# not a real sender identity -- MailHog accepts anything, and there's no
# domain to send "from" until a real provider exists (worker-decisions.md §8)
_FROM_ADDRESS = "no-reply@bookit.example"


def _format_cents(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _format_datetime(value: datetime) -> str:
    # drops microseconds and the +00:00 offset -- a guest reads "UTC", not
    # "2026-09-05 10:56:16.950438+00:00"
    return f"{value:%Y-%m-%d %H:%M} UTC"


# shared by all three send_* functions below
def _send(to: str, subject: str, body: str) -> None:
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = _FROM_ADDRESS
    message["To"] = to
    message.set_content(body)

    settings = get_settings()
    # blocking call -- callers await it via asyncio.to_thread, not directly
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.send_message(message)


# named "received", not "confirmed" -- fires while still PENDING, before
# confirm_reservation() (worker-decisions.md's BOOK-45 entries)
# BOOK-64: triggered by a BookingCreated event and built from its payload
# alone, same as confirmed/cancelled below. expires_at arrives as an ISO
# string, so it's parsed back before formatting.
async def send_reservation_received_email(payload: dict) -> None:
    guest, property_, reservation = payload["guest"], payload["property"], payload["reservation"]
    expires_at = datetime.fromisoformat(reservation["expires_at"])

    await asyncio.to_thread(
        _send,
        to=guest["email"],
        subject=f"Your reservation request for {property_['title']}",
        body=(
            f"Hi {guest['display_name'] or 'there'},\n\n"
            "We've received your reservation request:\n\n"
            f"Property: {property_['title']}, {property_['city']}, {property_['country']}\n"
            f"Dates: {reservation['check_in']} -> {reservation['check_out']}\n"
            f"Guests: {reservation['guest_count']}\n"
            f"Total: {_format_cents(reservation['total_cents'])}\n\n"
            f"This reservation is pending and held until {_format_datetime(expires_at)} -- "
            "it will be released automatically if not confirmed before then.\n"
        ),
    )


# BOOK-51. Triggered by the Kafka consumer, not called directly (§12/§13).
# BOOK-64: built from the event payload alone -- no reads of Booking's
# reservations/users/properties tables (microservices-decisions.md §5).
# Dates arrive as ISO strings ("2026-09-01"), which print the same as the
# date objects this used to read.
async def send_reservation_confirmed_email(payload: dict) -> None:
    guest, property_, reservation = payload["guest"], payload["property"], payload["reservation"]

    await asyncio.to_thread(
        _send,
        to=guest["email"],
        subject=f"Your reservation for {property_['title']} is confirmed",
        body=(
            f"Hi {guest['display_name'] or 'there'},\n\n"
            "Good news -- your reservation is confirmed:\n\n"
            f"Property: {property_['title']}, {property_['city']}, {property_['country']}\n"
            f"Dates: {reservation['check_in']} -> {reservation['check_out']}\n"
            f"Guests: {reservation['guest_count']}\n"
            f"Total: {_format_cents(reservation['total_cents'])}\n\n"
            "We look forward to hosting your stay.\n"
        ),
    )


# BOOK-51. Fires for both an explicit cancel (BOOK-38) and a silent
# auto-expiry (BOOK-46) -- the payload doesn't say which, so wording stays
# neutral rather than wrongly claiming "you cancelled this."
# BOOK-64: from the payload alone, same as the confirmed email above.
async def send_reservation_cancelled_email(payload: dict) -> None:
    guest, property_, reservation = payload["guest"], payload["property"], payload["reservation"]

    await asyncio.to_thread(
        _send,
        to=guest["email"],
        subject=f"Your reservation for {property_['title']} has been cancelled",
        body=(
            f"Hi {guest['display_name'] or 'there'},\n\n"
            "Your reservation is no longer active:\n\n"
            f"Property: {property_['title']}, {property_['city']}, {property_['country']}\n"
            f"Dates: {reservation['check_in']} -> {reservation['check_out']}\n\n"
            "These dates are now available again for other guests.\n"
        ),
    )
