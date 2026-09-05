"""The Celery application (BOOK-44) -- shared config point for both the API
(`.delay()`) and the worker (`app/worker.py`, which registers tasks here).

Why Celery on RabbitMQ, not arq on Redis: docs/jobs/worker-decisions.md §2.

`task_acks_late` is explicit, not left at Celery's ack-early default --
otherwise a worker crash mid-task loses the message silently instead of
RabbitMQ redelivering it. See docs/jobs/rabbitmq-basics.md §4.
"""

import asyncio
import functools
from collections.abc import Awaitable, Callable

from celery import Celery

from app.core.config import get_settings
from app.core.db.engine import get_engine

app = Celery("bookit", broker=get_settings().celery_broker_url)

app.conf.task_acks_late = True

# BOOK-46. Task referenced by name (string), not import -- importing
# app.worker here would be circular. Needs exactly one `celery beat`
# running; it doesn't dedupe itself (worker-decisions.md §2) -- don't scale
# the `beat` service the way `worker` can be scaled.
app.conf.beat_schedule = {
    "expire-pending-reservations": {
        "task": "app.worker.expire_pending_reservations",
        "schedule": 60.0,
    },
}


def async_task[T](async_fn: Callable[..., Awaitable[T]]) -> Callable[..., T]:
    """Bridges an async task body into Celery's sync task interface.

    Disposes the DB engine after each call, since a fresh event loop per
    call would otherwise reuse stale connections from the last one. See
    worker-decisions.md §2/6.
    """

    @functools.wraps(async_fn)
    async def run_and_dispose(*args: object, **kwargs: object) -> T:
        try:
            return await async_fn(*args, **kwargs)
        finally:
            await get_engine().dispose()

    @functools.wraps(async_fn)
    def wrapper(*args: object, **kwargs: object) -> T:
        return asyncio.run(run_and_dispose(*args, **kwargs))

    return wrapper
