"""BOOK-66: POST /payments when Booking can't be asked -- a 503 "try again",
and nothing charged or committed, so the same Idempotency-Key works on retry.
"""

from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from payment import booking_client
from payment.models import PaymentIdempotencyKey
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.integration.helpers import make_pending_reservation, payment_rows


def _unreachable() -> httpx.AsyncClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://booking")


@pytest.mark.anyio
async def test_booking_down_is_a_503_and_charges_nothing(
    payment_api_client: AsyncClient, db_session: AsyncSession, payment_db_session: AsyncSession
) -> None:
    reservation, guest = await make_pending_reservation(db_session)
    # what the gateway sets after checking the JWT (BOOK-67b)
    headers = {"X-User-Id": str(guest.id), "Idempotency-Key": "retry-me"}

    with patch.object(booking_client, "_client", _unreachable):
        response = await payment_api_client.post(
            "/payments", json={"reservation_id": str(reservation.id)}, headers=headers
        )

    assert response.status_code == 503
    assert await payment_rows(payment_db_session, reservation) == []  # no charge attempt at all
    # the key rolled back with everything else -- a retry with it starts fresh
    assert (
        await payment_db_session.scalar(select(func.count()).select_from(PaymentIdempotencyKey))
        == 0
    )
