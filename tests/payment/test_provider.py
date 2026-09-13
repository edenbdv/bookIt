import uuid

from app.payment.provider import PaymentOutcome, charge, refund

CID = uuid.uuid4()


def test_forced_outcome_is_returned_exactly() -> None:
    assert (
        charge(10_000, correlation_id=CID, force=PaymentOutcome.SUCCESS) == PaymentOutcome.SUCCESS
    )
    assert (
        charge(10_000, correlation_id=CID, force=PaymentOutcome.FAILURE) == PaymentOutcome.FAILURE
    )
    assert (
        charge(10_000, correlation_id=CID, force=PaymentOutcome.TIMEOUT) == PaymentOutcome.TIMEOUT
    )


def test_unforced_outcome_is_always_a_valid_member() -> None:
    for _ in range(200):
        assert charge(10_000, correlation_id=CID) in PaymentOutcome


def test_refund_forced_outcome_is_returned_exactly() -> None:
    assert (
        refund(10_000, correlation_id=CID, force=PaymentOutcome.SUCCESS) == PaymentOutcome.SUCCESS
    )
    assert (
        refund(10_000, correlation_id=CID, force=PaymentOutcome.FAILURE) == PaymentOutcome.FAILURE
    )
