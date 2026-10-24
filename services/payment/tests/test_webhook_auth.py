import pytest
from payment.webhook_auth import (
    InvalidWebhookSignatureError,
    sign_webhook_payload,
    verify_webhook_signature,
)

BODY = b'{"payment_id": "abc", "outcome": "success"}'


def test_valid_signature_verifies() -> None:
    verify_webhook_signature(BODY, sign_webhook_payload(BODY))  # doesn't raise


def test_tampered_body_is_rejected() -> None:
    signature = sign_webhook_payload(BODY)
    with pytest.raises(InvalidWebhookSignatureError):
        verify_webhook_signature(BODY.replace(b"success", b"failure"), signature)


def test_wrong_signature_is_rejected() -> None:
    with pytest.raises(InvalidWebhookSignatureError):
        verify_webhook_signature(BODY, "0" * 64)
