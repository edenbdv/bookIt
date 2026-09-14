"""The worker entrypoint (BOOK-44). Run with:

    uv run celery -A app.worker worker --loglevel=info

Celery discovers tasks by importing this module. See docs/jobs/celery-basics.md §2.
"""

import smtplib
import uuid
from datetime import UTC, datetime, timedelta

from app.core.db.engine import async_task
from app.core.db.unit_of_work import UnitOfWork
from app.core.idempotency import cleanup as idempotency_cleanup
from app.core.jobs.celery_app import app
from app.core.outbox import cleanup as outbox_cleanup
from app.core.outbox import relay as outbox_relay
from app.notification import service as notification_service
from app.payment import saga as payment_saga
from app.reservation import service as reservation_service

# BOOK-101: how long a payment sits PENDING before it's treated as stuck,
# not just still in flight
_STALE_PAYMENT_THRESHOLD = timedelta(minutes=5)

# BOOK-47: transient only (retry-basics.md §1). Excludes
# SMTPRecipientsRefused/SMTPSenderRefused -- a bad address won't fix itself.
_TRANSIENT_SMTP_ERRORS = (
    OSError,
    smtplib.SMTPConnectError,
    smtplib.SMTPServerDisconnected,
    smtplib.SMTPHeloError,
)


@app.task
def ping() -> str:
    """Smoke test: enqueue -> execute, touching nothing else (no DB, no external call)."""
    return "pong"


@app.task
@async_task
async def expire_pending_reservations() -> None:
    """BOOK-46: cancel PENDING reservations past expires_at.

    Thin wrapper -- opens the UnitOfWork a request would've, then delegates
    to reservation/service.py's testable, uow-accepting function.
    """
    async with UnitOfWork() as uow:
        await reservation_service.expire_pending_reservations(uow)


@app.task
@async_task
async def complete_past_reservations() -> None:
    """Unblocks BOOK-86: CONFIRMED -> COMPLETED past check_out.
    Thin wrapper, same shape as expire_pending_reservations above."""
    async with UnitOfWork() as uow:
        await reservation_service.complete_past_reservations(uow)


@app.task(
    autoretry_for=_TRANSIENT_SMTP_ERRORS,
    retry_backoff=True,
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=5,
)
@async_task
async def send_reservation_received_email(reservation_id: str) -> None:
    """BOOK-45/47: notify the guest their reservation request arrived.

    `reservation_id` is a string -- task args are JSON-serialized
    (celery-basics.md §6). Thin wrapper; logic lives in notification/service.py.

    Retries cover this task running and failing -- not the .delay() publish
    itself failing, which happens before any task exists (deliberately not
    retried, worker-decisions.md §9/§10).
    """
    async with UnitOfWork() as uow:
        await notification_service.send_reservation_received_email(uow, uuid.UUID(reservation_id))


@app.task
@async_task
async def reconcile_stale_payments() -> None:
    """BOOK-101: resolve PENDING payments the webhook never confirmed.
    Thin wrapper, same shape as expire_pending_reservations above."""
    async with UnitOfWork() as uow:
        await payment_saga.reconcile_stale_payments(
            uow, datetime.now(UTC) - _STALE_PAYMENT_THRESHOLD
        )


@app.task
@async_task
async def publish_outbox_events() -> None:
    """BOOK-50: the outbox relay. Publishes `outbox_events` rows nothing has
    sent to Kafka yet, oldest first; logic lives in core/outbox/relay.py.
    """
    producer = outbox_relay.make_producer()
    async with UnitOfWork() as uow:
        await outbox_relay.publish_pending_events(uow, producer)


@app.task
@async_task
async def delete_published_outbox_events() -> None:
    """BOOK-50: daily housekeeping, not publish-latency-sensitive like
    publish_outbox_events -- logic lives in core/outbox/cleanup.py.
    """
    async with UnitOfWork() as uow:
        await outbox_cleanup.delete_published_events(uow)


@app.task
@async_task
async def delete_expired_idempotency_keys() -> None:
    """Deferred from BOOK-36, built alongside BOOK-53 -- logic lives in
    core/idempotency/cleanup.py.
    """
    async with UnitOfWork() as uow:
        await idempotency_cleanup.delete_expired_keys(uow)
