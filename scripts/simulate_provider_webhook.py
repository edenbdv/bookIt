"""BOOK-55: manual stand-in for the real provider calling back.

`send_webhook()` is deliberately separate from the CLI below -- a later
production trigger (e.g. a Celery task scheduled after a TIMEOUT) can
import and call the same function instead of duplicating the signing.

Run with: uv run python scripts/simulate_provider_webhook.py <payment_id> <success|failure>
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app.payment.webhook_auth import sign_webhook_payload

BASE_URL = "http://localhost:8000"


def send_webhook(
    payment_id: uuid.UUID, outcome: str, *, base_url: str = BASE_URL
) -> httpx.Response:
    body = f'{{"payment_id": "{payment_id}", "outcome": "{outcome}"}}'.encode()
    signature = sign_webhook_payload(body)  # sign these exact bytes, not a re-serialized dict
    return httpx.post(
        f"{base_url}/payments/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
    )


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[2] not in ("success", "failure"):
        print(
            "Usage: uv run python scripts/simulate_provider_webhook.py <payment_id> <success|failure>"
        )
        sys.exit(1)

    response = send_webhook(uuid.UUID(sys.argv[1]), sys.argv[2])
    print(f"{response.status_code}: {response.text}")
