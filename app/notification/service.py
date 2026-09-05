"""BOOK-45: compose and send BookIt's transactional emails.

Deliberately the only thing this module knows how to do. What *triggers* a
call here is someone else's job -- today that's app/worker.py's Celery task,
calling this directly with a UnitOfWork it already opened. BOOK-51 (Epic 7)
replaces that caller with a Kafka consumer; nothing in this file needs to
change when that happens -- see docs/jobs/worker-decisions.md section 9.
"""

import smtplib
import uuid
from datetime import datetime
from email.message import EmailMessage

from app.core.config import get_settings
from app.core.db.unit_of_work import UnitOfWork

# not a real sender identity -- MailHog accepts anything, and there's no
# domain to send "from" until a real provider exists (worker-decisions.md §8)
_FROM_ADDRESS = "no-reply@bookit.example"


def _format_cents(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def _format_datetime(value: datetime) -> str:
    # drops microseconds and the +00:00 offset -- a guest reads "UTC", not
    # "2026-09-05 10:56:16.950438+00:00"
    return f"{value:%Y-%m-%d %H:%M} UTC"


# named for what this reservation actually is right now -- PENDING, not
# CONFIRMED. confirm_reservation() has no real caller yet (it's a manual
# stand-in for Epic 8's payment webhook), so nothing has actually been
# confirmed at the point this fires. See docs/jobs/worker-decisions.md's
# BOOK-45 entries for why "received" and "confirmed" are deliberately
# different emails, not one email with different wording.
async def send_reservation_received_email(uow: UnitOfWork, reservation_id: uuid.UUID) -> None:
    reservation = await uow.reservations.get_by_id(reservation_id)
    guest = await uow.users.get_by_id(reservation.guest_id)
    property_ = await uow.properties.get_by_id(reservation.property_id)

    message = EmailMessage()
    message["Subject"] = f"Your reservation request for {property_.title}"
    message["From"] = _FROM_ADDRESS
    message["To"] = guest.email
    message.set_content(
        f"Hi {guest.display_name or 'there'},\n\n"
        "We've received your reservation request:\n\n"
        f"Property: {property_.title}, {property_.city}, {property_.country}\n"
        f"Dates: {reservation.check_in} -> {reservation.check_out}\n"
        f"Guests: {reservation.guest_count}\n"
        f"Total: {_format_cents(reservation.total_cents)}\n\n"
        f"This reservation is pending and held until {_format_datetime(reservation.expires_at)} -- "
        "it will be released automatically if not confirmed before then.\n"
    )

    settings = get_settings()
    # sync smtplib, not an async client: this already runs inside
    # async_task's asyncio.run() bridge (core/jobs/celery_app.py), and one
    # blocking SMTP call per task is the cost that bridge exists to isolate
    # -- see docs/jobs/background-jobs-basics.md §3 on why a blocking call
    # would be destructive inside a *shared* event loop, which this isn't.
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.send_message(message)
