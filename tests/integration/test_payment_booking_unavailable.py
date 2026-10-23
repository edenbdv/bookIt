"""BOOK-66: POST /payments when Booking can't be asked -- a 503 "try again",
and nothing charged or committed, so the same Idempotency-Key works on retry.
"""

from unittest.mock import patch

import httpx
import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.idempotency.models import IdempotencyKey
from app.payment import booking_client
from tests.integration.helpers import auth_headers, make_pending_reservation, payment_rows


def _unreachable() -> httpx.AsyncClient:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(refuse), base_url="http://booking")


@pytest.mark.anyio
async def test_booking_down_is_a_503_and_charges_nothing(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    reservation, guest = await make_pending_reservation(db_session)
    headers = auth_headers(guest, idempotency_key="retry-me")

    with patch.object(booking_client, "_client", _unreachable):
        response = await api_client.post(
            "/payments", json={"reservation_id": str(reservation.id)}, headers=headers
        )

    assert response.status_code == 503
    assert await payment_rows(db_session, reservation) == []  # no charge attempt at all
    # the key rolled back with everything else -- a retry with it starts fresh
    assert await db_session.scalar(select(func.count()).select_from(IdempotencyKey)) == 0
