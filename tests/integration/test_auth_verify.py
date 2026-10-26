"""BOOK-67b: GET /auth/verify -- what the gateway's forward auth asks before
guest /payments requests. 2xx lets the request through with X-User-Id copied
onto it; anything else is sent back to the browser as is."""

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.security import create_access_token, create_refresh_token, new_id
from tests.integration.helpers import auth_headers, make_user


@pytest.mark.anyio
async def test_a_valid_access_token_returns_the_user_id(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, "guest@example.com")
    await db_session.commit()

    response = await api_client.get("/auth/verify", headers=auth_headers(user))

    assert response.status_code == 204
    assert response.headers["X-User-Id"] == str(user.id)


@pytest.mark.anyio
async def test_no_token_is_rejected(api_client: AsyncClient) -> None:
    response = await api_client.get("/auth/verify")

    assert response.status_code == 401
    assert "X-User-Id" not in response.headers


@pytest.mark.anyio
async def test_a_refresh_token_is_rejected(
    api_client: AsyncClient, db_session: AsyncSession
) -> None:
    user = await make_user(db_session, "guest@example.com")
    await db_session.commit()
    token = create_refresh_token(user.id, new_id(), new_id())

    response = await api_client.get("/auth/verify", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401


# a valid, unexpired token for a user who no longer exists -- Payment's own
# check accepted this until the token expired (§19); the gateway now doesn't
@pytest.mark.anyio
async def test_a_token_for_a_missing_user_is_rejected(api_client: AsyncClient) -> None:
    token = create_access_token(uuid.uuid4(), new_id())

    response = await api_client.get("/auth/verify", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
