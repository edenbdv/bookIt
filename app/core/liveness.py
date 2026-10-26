"""BOOK-105: a Kafka consumer can stop consuming without dying -- the process stays
up, so `restart: unless-stopped` never kicks in (seen 2026-10-24: out of its group
for 2+ hours after a coordinator hiccup). A watchdog thread checks two things and,
if either fails, exits the process so Docker restarts it.
"""

import logging
import os
import threading
import time

from confluent_kafka import Consumer

logger = logging.getLogger(__name__)

# the loop polls every ~1s (plus a few seconds of retries, BOOK-68) -- 30s is far past that
LOOP_STALE_SECONDS = 30.0
# a normal rebalance takes seconds
UNASSIGNED_LIMIT_SECONDS = 120.0
CHECK_EVERY_SECONDS = 10.0


def stuck_reason(now: float, last_poll_at: float, unassigned_since: float | None) -> str | None:
    if now - last_poll_at > LOOP_STALE_SECONDS:
        return f"no poll for {now - last_poll_at:.0f}s"
    if unassigned_since is not None and now - unassigned_since > UNASSIGNED_LIMIT_SECONDS:
        return f"out of its consumer group for {now - unassigned_since:.0f}s"
    return None


class Liveness:
    """What the loop last did, for the watchdog to judge. Starts unassigned:
    joining the group takes a moment, and never joining counts too."""

    def __init__(self, now: float | None = None) -> None:
        start = time.monotonic() if now is None else now
        self.last_poll_at = start
        self.unassigned_since: float | None = start

    def polled(self, consumer: Consumer, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.last_poll_at = now
        if consumer.assignment():
            self.unassigned_since = None
        elif self.unassigned_since is None:
            self.unassigned_since = now

    def start_watchdog(self) -> None:
        threading.Thread(target=self._watch, name="liveness-watchdog", daemon=True).start()

    def _watch(self) -> None:
        while True:
            time.sleep(CHECK_EVERY_SECONDS)
            reason = stuck_reason(time.monotonic(), self.last_poll_at, self.unassigned_since)
            if reason:
                logger.critical("Consumer stuck (%s) -- exiting so Docker restarts it", reason)
                # os._exit, not sys.exit: from a thread, sys.exit ends only that thread
                os._exit(1)
