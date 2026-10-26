"""BOOK-105: when a consumer counts as stuck -- its loop stopped polling, or it's
been out of its group too long. Plain numbers in, a reason (or None) out."""

from app.core.liveness import Liveness, stuck_reason

NOW = 1000.0


def test_a_polling_consumer_in_its_group_is_fine() -> None:
    assert stuck_reason(NOW, last_poll_at=NOW - 2, unassigned_since=None) is None


def test_a_loop_that_stopped_polling_is_stuck() -> None:
    assert "poll" in stuck_reason(NOW, last_poll_at=NOW - 31, unassigned_since=None)


def test_out_of_the_group_too_long_is_stuck() -> None:
    # the 2026-10-24 stall: polling fine, but never back in the group
    assert "group" in stuck_reason(NOW, last_poll_at=NOW - 1, unassigned_since=NOW - 121)


def test_a_short_rebalance_is_tolerated() -> None:
    assert stuck_reason(NOW, last_poll_at=NOW - 1, unassigned_since=NOW - 10) is None


class _Consumer:
    def __init__(self, assigned: bool) -> None:
        self.assigned = assigned

    def assignment(self) -> list:
        return ["partition"] if self.assigned else []


def test_polled_tracks_when_the_assignment_went_empty() -> None:
    liveness = Liveness(now=0.0)  # starts unassigned -- joining the group takes a moment

    liveness.polled(_Consumer(assigned=True), now=5.0)
    assert (liveness.last_poll_at, liveness.unassigned_since) == (5.0, None)

    liveness.polled(_Consumer(assigned=False), now=8.0)
    liveness.polled(_Consumer(assigned=False), now=9.0)
    assert liveness.unassigned_since == 8.0  # since it first went empty, not the latest poll
