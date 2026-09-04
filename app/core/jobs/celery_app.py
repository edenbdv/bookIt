"""The Celery application (BOOK-44) -- the single config point shared by both
sides of the queue: the API calls `.delay()` through this object, and the
worker process (`app/worker.py`) registers tasks against this same object.

Why Celery on RabbitMQ, not arq on Redis: docs/jobs/worker-decisions.md
section 2.

`task_acks_late` is set explicitly, not left at Celery's default. Celery's
default acks a task the instant it's *received*, before it runs -- so a
worker crash mid-task loses the message silently, with no redelivery. That
default throws away the entire reason this project chose RabbitMQ over Redis
in the first place. See docs/jobs/rabbitmq-basics.md section 4 for the
early-ack-vs-late-ack distinction, and why "when to ack" is the worker's
decision while "redeliver an unacked message" is the broker's.
"""

from celery import Celery

from app.core.config import get_settings

app = Celery("bookit", broker=get_settings().celery_broker_url)

app.conf.task_acks_late = True
