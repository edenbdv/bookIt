"""BOOK-65: Payment's housekeeping -- the same two jobs Booking runs for its own
tables (core/outbox/cleanup.py, core/idempotency/cleanup.py), for Payment's.
Run daily by payment-scheduler. Housekeeping, not publishing: kept apart from
relay.py and service.py on purpose.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy import delete

from payment.db import PaymentUnitOfWork
from payment.models import PaymentIdempotencyKey, PaymentOutboxEvent

# same window as Booking's outbox: long enough to debug a recent delivery issue --
# Kafka keeps its own copy of every published event anyway
OUTBOX_RETENTION_DAYS = 7


async def delete_published_events(uow: PaymentUnitOfWork) -> int:
    """Published payment events older than the retention window. Unpublished rows
    are never touched -- the relay still has to send them."""
    cutoff = datetime.now(UTC) - timedelta(days=OUTBOX_RETENTION_DAYS)
    result = await uow.session.execute(
        delete(PaymentOutboxEvent).where(
            PaymentOutboxEvent.published_at.is_not(None),
            PaymentOutboxEvent.published_at < cutoff,
        )
    )
    return result.rowcount


async def delete_expired_keys(uow: PaymentUnitOfWork) -> int:
    """Payment idempotency keys past their own expires_at (~24h)."""
    result = await uow.session.execute(
        delete(PaymentIdempotencyKey).where(PaymentIdempotencyKey.expires_at < datetime.now(UTC))
    )
    return result.rowcount
