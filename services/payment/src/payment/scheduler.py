"""BOOK-65: Payment's scheduled jobs, in its own process. Run with:

    uv run python -m payment.scheduler

A simple loop, not Celery (microservices-decisions.md §20): Payment has two fixed
periodic jobs and no on-demand tasks. One asyncio.run() for the process's whole
lifetime, so one engine for every run. Run ONE instance -- two would run each job
twice (harmless, just wasteful).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from confluent_kafka import Producer

from payment import relay, service
from payment.config import get_payment_settings
from payment.db import PaymentUnitOfWork, get_payment_engine

logger = logging.getLogger(__name__)

TICK_SECONDS = 5.0
# BOOK-101: how long a payment sits PENDING before it's treated as stuck
STALE_PAYMENT_THRESHOLD = timedelta(minutes=5)


@dataclass
class Job:
    name: str
    every: timedelta
    run: Callable[[], Awaitable[None]]
    next_due: datetime = field(default_factory=lambda: datetime.min.replace(tzinfo=UTC))


def make_jobs(producer: Producer) -> list[Job]:
    async def publish_events() -> None:
        async with PaymentUnitOfWork() as uow:
            await relay.publish_pending_payment_events(uow, producer)

    async def reconcile() -> None:
        async with PaymentUnitOfWork() as uow:
            await service.reconcile_stale_payments(uow, datetime.now(UTC) - STALE_PAYMENT_THRESHOLD)

    return [
        # same cadences as Booking's beat had: the relay's delay is user-facing
        # (how long until a reservation is confirmed), reconcile isn't
        Job("publish-payment-events", timedelta(seconds=5), publish_events),
        Job("reconcile-stale-payments", timedelta(seconds=60), reconcile),
    ]


async def run_due(jobs: list[Job], now: datetime) -> None:
    """Run every job that's due, one at a time. A failure is logged and the job is
    rescheduled as usual -- the next run simply tries again; never stops the loop."""
    for job in jobs:
        if now < job.next_due:
            continue
        job.next_due = now + job.every
        try:
            await job.run()
        except Exception:
            logger.exception("Scheduled job %s failed -- retried on its next run", job.name)


async def _run() -> None:
    producer = Producer({"bootstrap.servers": get_payment_settings().kafka_bootstrap_servers})
    jobs = make_jobs(producer)
    try:
        while True:
            await run_due(jobs, datetime.now(UTC))
            await asyncio.sleep(TICK_SECONDS)
    finally:
        await get_payment_engine().dispose()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()
