"""BOOK-65: Payment's own app -- its routes and health check, apart from Booking's."""

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_health_checks_payments_own_database(payment_api_client: AsyncClient) -> None:
    response = await payment_api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"database": "ok"}


# only Payment's routes -- nothing of Booking's leaks into this app
@pytest.mark.anyio
async def test_serves_only_payment_routes(payment_api_client: AsyncClient) -> None:
    paths = (await payment_api_client.get("/openapi.json")).json()["paths"]

    assert sorted(paths) == ["/health", "/payments", "/payments/webhook"]
