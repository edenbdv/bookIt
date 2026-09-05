"""The worker entrypoint (BOOK-44). Run with:

    uv run celery -A app.worker worker --loglevel=info

Celery discovers tasks by importing this module. See docs/jobs/celery-basics.md §2.
"""

import uuid

from app.core.db.unit_of_work import UnitOfWork
from app.core.jobs.celery_app import app, async_task
from app.notification import service as notification_service
from app.reservation import service as reservation_service


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
async def send_reservation_received_email(reservation_id: str) -> None:
    """BOOK-45: notify the guest their reservation request arrived.

    `reservation_id` is a string -- task args are JSON-serialized
    (celery-basics.md §6). Thin wrapper; logic lives in
    notification/service.py, the part BOOK-51 keeps unchanged when this
    trigger becomes a Kafka consumer (worker-decisions.md §9).
    """
    async with UnitOfWork() as uow:
        await notification_service.send_reservation_received_email(uow, uuid.UUID(reservation_id))
