"""The worker entrypoint (BOOK-44). Run with:

    uv run celery -A app.worker worker --loglevel=info

Celery discovers tasks by importing this module. See docs/jobs/celery-basics.md §2.
"""

from app.core.db.engine import async_task
from app.core.db.unit_of_work import UnitOfWork
from app.core.idempotency import cleanup as idempotency_cleanup
from app.core.jobs.celery_app import app
from app.core.outbox import cleanup as outbox_cleanup
from app.core.outbox import relay as outbox_relay
from app.reservation import service as reservation_service

# BOOK-64: the "reservation received" email task (BOOK-45/47) used to live
# here. It moved to Notification's Kafka consumer as a BookingCreated event
# (microservices-decisions.md §4) -- this worker no longer sends any email.
#
# BOOK-65: Payment's two jobs (reconcile_stale_payments, publish_payment_events)
# moved to Payment's own payment-scheduler (services/payment/src/payment/scheduler.py, §20).


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
