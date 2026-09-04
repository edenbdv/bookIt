"""The worker entrypoint (BOOK-44). Run it with:

    uv run celery -A app.worker worker --loglevel=info

`main.py` serves HTTP; this is the process that runs code with no request
behind it. Celery discovers tasks by importing this module -- `@app.task`
below registers `ping` in the task registry the moment that import happens.
See docs/jobs/celery-basics.md section 2 for what that registration actually
does, and docs/jobs/rabbitmq-basics.md for what happens underneath it once a
message reaches RabbitMQ.
"""

import time

from app.core.jobs.celery_app import app


@app.task
def ping() -> str:
    """Smoke test proving enqueue -> execute works end to end.

    Deliberately touches nothing: no database, no external service. When
    this runs and a real job doesn't, the problem is the job, not the
    plumbing -- same reasoning as choosing the expiry job first in
    docs/jobs/worker-decisions.md section 5, one step earlier.
    """
    # TEMPORARY (not committed): slowed down on purpose so the ack window
    # is visible in RabbitMQ's UI as "Unacked: 1" for a few seconds. Revert
    # to `return "pong"` right after.
    time.sleep(10)
    return "pong"
