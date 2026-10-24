"""BOOK-65: get_guest_id -- Payment reads who is paying from the guest's access
token alone, with no users-table lookup. Tokens are built here from the contract --
the claims Booking's login puts in an access token (sub, type, exp) -- without
importing Booking's code. Real login tokens are covered by the cross-service tests in
tests/integration/, which pay through Payment's app.
"""

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from payment.auth import get_guest_id
from payment.config import get_payment_settings


def _bearer(token: str) -> HTTPAuthorizationCredentials:
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def _signed(payload: dict, secret: str | None = None) -> str:
    settings = get_payment_settings()
    return jwt.encode(payload, secret or settings.secret_key, algorithm=settings.jwt_algorithm)


def _token(user_id: uuid.UUID, type_: str = "access", expires_in_min: int = 15) -> str:
    # the shape of Booking's login tokens -- the contract Payment accepts
    return _signed(
        {
            "sub": str(user_id),
            "type": type_,
            "exp": datetime.now(UTC) + timedelta(minutes=expires_in_min),
        }
    )


@pytest.mark.anyio
async def test_a_login_access_token_gives_the_user_id() -> None:
    user_id = uuid.uuid4()

    assert await get_guest_id(_bearer(_token(user_id))) == user_id


@pytest.mark.anyio
@pytest.mark.parametrize(
    "token",
    [
        pytest.param(_token(uuid.uuid4(), type_="refresh"), id="a refresh token"),
        pytest.param(
            _signed(
                {
                    "sub": "payment",
                    "type": "service",
                    "exp": datetime.now(UTC) + timedelta(minutes=5),
                },
                get_payment_settings().service_token_secret,
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
