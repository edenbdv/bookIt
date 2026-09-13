"""BOOK-76: how much a guest gets back, based on the property's
cancellation_policy and how far ahead of check_in they cancel.

Cutoffs (inclusive -- "up to N before" means >= N counts as still ahead):

    FLEXIBLE: 100% at >=24h before check-in, else 0%
    MODERATE: 100% at >=5 days, 50% at >=24h, else 0%
    STRICT:   50% at >=7 days, else 0%

check_in has no time-of-day (Reservation.check_in is a date) -- treated as
midnight UTC of that date for this calculation, a deliberate simplification
rather than an assumed check-in hour.
"""

from datetime import UTC, date, datetime, time

from app.property.models import CancellationPolicy

_HOURS_PER_DAY = 24


def _hours_until_check_in(check_in: date, cancelled_at: datetime) -> float:
    check_in_start = datetime.combine(check_in, time.min, tzinfo=UTC)
    return (check_in_start - cancelled_at).total_seconds() / 3600


def calculate_refund_cents(
    policy: CancellationPolicy, check_in: date, cancelled_at: datetime, total_cents: int
) -> int:
    hours_until = _hours_until_check_in(check_in, cancelled_at)

    if policy == CancellationPolicy.FLEXIBLE:
        return total_cents if hours_until >= _HOURS_PER_DAY else 0

    if policy == CancellationPolicy.MODERATE:
        if hours_until >= 5 * _HOURS_PER_DAY:
            return total_cents
        if hours_until >= _HOURS_PER_DAY:
            return total_cents // 2
        return 0

    if policy == CancellationPolicy.STRICT:
        return total_cents // 2 if hours_until >= 7 * _HOURS_PER_DAY else 0

    raise AssertionError(f"unhandled cancellation policy {policy!r}")
