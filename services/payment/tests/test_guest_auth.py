"""BOOK-67b: get_guest_id -- Payment reads who is paying from X-User-Id, set by the
gateway after Booking's /auth/verify checked the JWT. The token checks themselves
(refresh, expired, wrong secret...) are Booking's tests now
(tests/integration/test_auth_verify.py).
"""

import uuid

import pytest
from fastapi import HTTPException
from payment.auth import get_guest_id


@pytest.mark.anyio
async def test_the_gateways_header_gives_the_user_id() -> None:
    user_id = uuid.uuid4()

    assert await get_guest_id(str(user_id)) == user_id


# no header, or not a uuid -- the request didn't come through the gateway
@pytest.mark.anyio
@pytest.mark.parametrize(
    "header",
    [pytest.param(None, id="missing"), pytest.param("not-a-uuid", id="not a uuid")],
)
async def test_anything_but_a_user_id_is_401(header: str | None) -> None:
    with pytest.raises(HTTPException) as exc_info:
        await get_guest_id(header)

    assert exc_info.value.status_code == 401
