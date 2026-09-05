"""The worker entrypoint (BOOK-44). Run it with:

    uv run celery -A app.worker worker --loglevel=info

`main.py` serves HTTP; this is the process that runs code with no request
behind it. Celery discovers tasks by importing this module -- `@app.task`
below registers `ping` in the task registry the moment that import happens.
See docs/jobs/celery-basics.md section 2 for what that registration actually
does, and docs/jobs/rabbitmq-basics.md for what happens underneath it once a
message reaches RabbitMQ.
"""

from app.core.db.unit_of_work import UnitOfWork
from app.core.jobs.celery_app import app, async_task
from app.reservation import service


@app.task
def ping() -> str:
    """Smoke test proving enqueue -> execute works end to end.

    Deliberately touches nothing: no database, no external service. When
    this runs and a real job doesn't, the problem is the job, not the
    plumbing -- same reasoning as choosing the expiry job first in
    docs/jobs/worker-decisions.md section 5, one step earlier.
    """
    return "pong"


@app.task
@async_task
async def expire_pending_reservations() -> None:
    """BOOK-46: cancel PENDING reservations past their expires_at.

    Thin wrapper only -- constructs the real UnitOfWork (there's no request
    here for Depends(get_uow) to do it), then delegates to
    reservation/service.py's uow-accepting function, same shape the router
    uses for cancel_reservation/confirm_reservation. Keeps the actual logic
    testable with a FakeUnitOfWork, no DB needed.
    """
    async with UnitOfWork() as uow:
        await service.expire_pending_reservations(uow)
