"""BOOK-55: HMAC verification for inbound webhook calls -- see
docs/payment/webhook-basics.md §8. No user session to check here; a
signature over the raw body is what proves the caller is the real
provider, not just anyone who found the URL.
"""

import hashlib
import hmac

from app.core.config import get_settings


class InvalidWebhookSignatureError(Exception):
    pass


def sign_webhook_payload(raw_body: bytes) -> str:
    # same function signs (caller side) and verifies (receiver side) below
    secret = get_settings().payment_webhook_secret.encode()
    return hmac.new(secret, raw_body, hashlib.sha256).hexdigest()


def verify_webhook_signature(raw_body: bytes, signature: str) -> None:
    # compare_digest, not == -- constant-time, avoids leaking match length via timing
    if not hmac.compare_digest(sign_webhook_payload(raw_body), signature):
        raise InvalidWebhookSignatureError
