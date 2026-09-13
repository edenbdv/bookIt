from datetime import UTC, date, datetime, timedelta

import pytest

from app.property.models import CancellationPolicy
from app.reservation.cancellation_policy import calculate_refund_cents

CHECK_IN = date(2026, 6, 15)
CHECK_IN_START = datetime.combine(CHECK_IN, datetime.min.time(), tzinfo=UTC)
TOTAL_CENTS = 40_000


def _cancel(hours_before: float) -> datetime:
    return CHECK_IN_START - timedelta(hours=hours_before)


@pytest.mark.parametrize(
    ("hours_before", "expected"),
    [
        (48, TOTAL_CENTS),  # well ahead
        (24, TOTAL_CENTS),  # exactly the cutoff -- still counts as ahead
        (23.999, 0),  # just inside the cutoff
        (1, 0),
        (0, 0),  # cancelling at check-in itself
    ],
)
def test_flexible(hours_before: float, expected: int) -> None:
    assert (
        calculate_refund_cents(
            CancellationPolicy.FLEXIBLE, CHECK_IN, _cancel(hours_before), TOTAL_CENTS
        )
        == expected
    )


@pytest.mark.parametrize(
    ("hours_before", "expected"),
    [
        (10 * 24, TOTAL_CENTS),  # well past the 5-day tier
        (5 * 24, TOTAL_CENTS),  # exactly 5 days -- still full
        (5 * 24 - 0.001, TOTAL_CENTS // 2),  # just inside 5 days -- drops to half
        (48, TOTAL_CENTS // 2),
        (24, TOTAL_CENTS // 2),  # exactly 24h -- still half
        (23.999, 0),  # just inside 24h -- drops to nothing
        (0, 0),
    ],
)
def test_moderate(hours_before: float, expected: int) -> None:
    assert (
        calculate_refund_cents(
            CancellationPolicy.MODERATE, CHECK_IN, _cancel(hours_before), TOTAL_CENTS
        )
        == expected
    )


@pytest.mark.parametrize(
    ("hours_before", "expected"),
    [
        (10 * 24, TOTAL_CENTS // 2),
        (7 * 24, TOTAL_CENTS // 2),  # exactly 7 days -- still half
        (7 * 24 - 0.001, 0),  # just inside 7 days -- drops to nothing
        (24, 0),
        (0, 0),
    ],
)
def test_strict(hours_before: float, expected: int) -> None:
    assert (
        calculate_refund_cents(
            CancellationPolicy.STRICT, CHECK_IN, _cancel(hours_before), TOTAL_CENTS
        )
        == expected
    )


def test_cancelling_after_check_in_still_refunds_nothing() -> None:
    # negative hours_before == cancelling after check_in already started
    assert (
        calculate_refund_cents(CancellationPolicy.FLEXIBLE, CHECK_IN, _cancel(-5), TOTAL_CENTS) == 0
    )
