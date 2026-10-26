"""BOOK-68: _with_retry -- a passing DB error gets more tries before the DLQ;
anything else goes there straight away. No Kafka, no DB: a fake attempt."""

from unittest.mock import patch

import pytest
from sqlalchemy.exc import OperationalError

from app.reservation import payment_consumer


class _Attempt:
    """Fails with each error in `errors`, in order, then succeeds."""

    def __init__(self, *errors: Exception) -> None:
        self.errors = list(errors)
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)


def _db_down() -> OperationalError:
    return OperationalError("SELECT 1", {}, ConnectionRefusedError("connection refused"))


@pytest.fixture(autouse=True)
def _no_waiting():
    with patch.object(payment_consumer, "RETRY_DELAYS", (0.0, 0.0)):
        yield


@pytest.mark.anyio
async def test_a_passing_db_error_is_retried_and_then_succeeds() -> None:
    attempt = _Attempt(_db_down())

    await payment_consumer._with_retry(attempt)

    assert attempt.calls == 2


@pytest.mark.anyio
async def test_a_db_error_on_every_try_is_raised_after_three() -> None:
    attempt = _Attempt(_db_down(), _db_down(), _db_down())

    with pytest.raises(OperationalError):
        await payment_consumer._with_retry(attempt)

    assert attempt.calls == 3


# a bad message fails the same way every time -- no point waiting
@pytest.mark.anyio
async def test_a_permanent_error_is_raised_at_once() -> None:
    attempt = _Attempt(KeyError("reservation_id"))

    with pytest.raises(KeyError):
        await payment_consumer._with_retry(attempt)

    assert attempt.calls == 1
