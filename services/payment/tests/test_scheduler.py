"""BOOK-65: payment-scheduler's run_due -- which jobs run when, and that one failing
job never stops the others or the loop."""

from datetime import UTC, datetime, timedelta

import pytest
from payment.scheduler import Job, run_due

T0 = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


def _job(name: str, every_s: int, calls: list[str], fail: bool = False) -> Job:
    async def run() -> None:
        calls.append(name)
        if fail:
            raise RuntimeError("boom")

    return Job(name, timedelta(seconds=every_s), run)


@pytest.mark.anyio
async def test_every_job_runs_on_the_first_tick() -> None:
    calls: list[str] = []
    jobs = [_job("relay", 5, calls), _job("reconcile", 60, calls)]

    await run_due(jobs, T0)

    assert calls == ["relay", "reconcile"]


@pytest.mark.anyio
async def test_each_job_runs_on_its_own_interval() -> None:
    calls: list[str] = []
    jobs = [_job("relay", 5, calls), _job("reconcile", 60, calls)]
    await run_due(jobs, T0)
    calls.clear()

    await run_due(jobs, T0 + timedelta(seconds=5))  # relay due, reconcile not
    await run_due(jobs, T0 + timedelta(seconds=7))  # neither due
    await run_due(jobs, T0 + timedelta(seconds=60))  # both due

    assert calls == ["relay", "relay", "reconcile"]


@pytest.mark.anyio
async def test_a_failing_job_does_not_stop_the_others() -> None:
    calls: list[str] = []
    jobs = [_job("relay", 5, calls, fail=True), _job("reconcile", 60, calls)]

    await run_due(jobs, T0)  # must not raise

    assert calls == ["relay", "reconcile"]
    # and the failed one is rescheduled as usual -- tried again on its next run
    await run_due(jobs, T0 + timedelta(seconds=5))
    assert calls == ["relay", "reconcile", "relay"]
