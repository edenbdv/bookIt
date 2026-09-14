"""The Celery application (BOOK-44) -- shared config point for both the API
(`.delay()`) and the worker (`app/worker.py`, which registers tasks here).

Why Celery on RabbitMQ, not arq on Redis: docs/jobs/worker-decisions.md §2.

`task_acks_late` is explicit, not left at Celery's ack-early default --
otherwise a worker crash mid-task loses the message silently instead of
RabbitMQ redelivering it. See docs/jobs/rabbitmq-basics.md §4.
"""

from datetime import UTC, datetime

from celery import Celery
from celery.schedules import crontab
from celery.signals import task_failure
from kombu import Queue

from app.core.config import get_settings

app = Celery("bookit", broker=get_settings().celery_broker_url)

app.conf.task_acks_late = True

# BOOK-47. RabbitMQ's native DLX doesn't apply here -- Celery ACKs a task
# even on failure, never nacks, so a queue-level DLX never triggers.
# task_failure fires once a task is *finally* failed (autoretry_for
# exhausted or not), which is what actually catches this. worker-decisions.md §10.
_DEAD_LETTER_QUEUE = Queue("notifications.dlq", durable=True)


@task_failure.connect
def _send_to_dead_letter(
    sender=None,
    task_id=None,
    exception=None,
    args=None,
    kwargs=None,
    einfo=None,
    **_extra,
) -> None:
    # retries: sender.request is still the live task context at this point
    # (the signal fires from inside the failed task's own execution).
    # einfo.traceback: Celery's own pre-formatted string, not the raw
    # traceback object the signal also carries -- that one needs the stdlib
    # `traceback` module to format, einfo already did it.
    request = getattr(sender, "request", None)
    with app.connection_for_write() as connection:
        connection.Producer().publish(
            {
                "task": getattr(sender, "name", str(sender)),
                "task_id": task_id,
                "args": args,
                "kwargs": kwargs,
                "exception": str(exception),
                "traceback": einfo.traceback if einfo else None,
                "retries": getattr(request, "retries", None),
                "timestamp": datetime.now(UTC).isoformat(),
            },
            routing_key=_DEAD_LETTER_QUEUE.name,
            declare=[_DEAD_LETTER_QUEUE],
            serializer="json",
            retry=True,
        )


# BOOK-46. Task referenced by name (string), not import -- importing
# app.worker here would be circular. Needs exactly one `celery beat`
# running; it doesn't dedupe itself (worker-decisions.md §2) -- don't scale
# the `beat` service the way `worker` can be scaled.
app.conf.beat_schedule = {
    "expire-pending-reservations": {
        "task": "app.worker.expire_pending_reservations",
        "schedule": 60.0,
    },
    # BOOK-101. Only acts on payments past _STALE_PAYMENT_THRESHOLD (5 min).
    "reconcile-stale-payments": {
        "task": "app.worker.reconcile_stale_payments",
        "schedule": 60.0,
    },
    # BOOK-50. Deliberately more frequent than the expiry job -- publish
    # latency here is user-facing (a guest's confirmation/cancellation
    # notification), not an internal cleanup, so it's worth a tighter poll
    # (worker-decisions.md §11).
    "publish-outbox-events": {
        "task": "app.worker.publish_outbox_events",
        "schedule": 5.0,
    },
    # BOOK-50's last step. Housekeeping, not latency-sensitive like the
    # relay above -- a daily crontab, not a tight interval, is the right
    # tool here (outbox-basics.md/BOOK-50: published rows are redundant
    # with Kafka's own retention once they're old enough to no longer help
    # debug a recent delivery issue).
    "delete-published-outbox-events": {
        "task": "app.worker.delete_published_outbox_events",
        "schedule": crontab(hour=3, minute=0),
    },
    # deferred from BOOK-36. Daily is fine, but not for the same reason as
    # the outbox cleanup above -- an un-cleaned expired key isn't inert: a
    # client reusing that exact key for a genuinely new request would get
    # back the old result instead of a new payment. Rare (keys are normally
    # single-use, e.g. a fresh UUID per attempt) and safe either way (no
    # double charge, just a stale-but-correct answer) -- not zero impact,
    # just low-severity enough that daily is still the right cadence.
    "delete-expired-idempotency-keys": {
        "task": "app.worker.delete_expired_idempotency_keys",
        "schedule": crontab(hour=3, minute=30),
    },
}
