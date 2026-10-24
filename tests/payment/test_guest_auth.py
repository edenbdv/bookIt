"""BOOK-65: get_guest_id -- Payment reads who is paying from the guest's access
token alone, with no users-table lookup. Tokens are minted the way Booking's login
does (app.auth.security), since that's the contract Payment has to accept.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials

from app.auth.security import create_access_token, create_refresh_token, new_id
from app.core.config import get_settings
from app.payment.auth import get_guest_id


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


@pytest.mark.anyio
async def test_a_login_access_token_gives_the_user_id() -> None:
    user_id = uuid.uuid4()

    assert await get_guest_id(_bearer(create_access_token(user_id, new_id()))) == user_id


def _signed(payload: dict, secret: str | None = None) -> str:
    settings = get_settings()
    return jwt.encode(payload, secret or settings.secret_key, algorithm=settings.jwt_algorithm)


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token",
    [
        pytest.param(create_refresh_token(uuid.uuid4(), new_id(), new_id()), id="a refresh token"),
        pytest.param(
            _signed(
                {
                    "sub": "payment",
                    "type": "service",
                    "exp": datetime.now(UTC) + timedelta(minutes=5),
                },
                get_settings().service_token_secret,
            ),
            id="a service token",
        ),
        pytest.param(
            _signed(
                {
                    "sub": str(uuid.uuid4()),
                    "type": "access",
                    "exp": datetime.now(UTC) - timedelta(minutes=1),
                }
            ),
            id="expired",
        ),
        pytest.param(
            _signed(
                {
                    "sub": str(uuid.uuid4()),
                    "type": "access",
                    "exp": datetime.now(UTC) + timedelta(minutes=5),
                },
                "some-other-secret",
            ),
            id="wrong secret",
        ),
        pytest.param("not-a-jwt", id="garbage"),
    ],
)
async def test_anything_but_a_valid_access_token_is_401(token: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_guest_id(_bearer(token))

    assert exc_info.value.status_code == 401
