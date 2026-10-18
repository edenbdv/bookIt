"""BOOK-61 smoke test for the API-level fixtures: GET /health goes through
the real app and its own cached engine + Redis client -- so a 200 here proves
the whole app is wired to the test containers, not to the dev DB/Redis."""

import pytest
from httpx import AsyncClient


@pytest.mark.anyio
async def test_health_reaches_test_containers(api_client: AsyncClient) -> None:
    response = await api_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"database": "ok", "redis": "ok"}


# a second request in a second test -- would fail with "attached to a
# different loop" if the cached engine/Redis client survived the first
# test's event loop (testing-basics.md §3.5)
@pytest.mark.anyio
async def test_health_works_again_in_a_new_event_loop(api_client: AsyncClient) -> None:
    response = await api_client.get("/health")

    assert response.status_code == 200
