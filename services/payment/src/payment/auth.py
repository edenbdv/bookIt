"""BOOK-67b: who is paying -- the gateway already checked the guest's JWT (forward
auth to Booking's /auth/verify) and set X-User-Id from it. Safe to trust only because
nothing reaches Payment except through the gateway, which strips any client-sent
X-User-Id (api-gateway-basics.md §4).
"""

import uuid

from fastapi import Header, HTTPException, status


async def get_guest_id(x_user_id: str | None = Header(default=None)) -> uuid.UUID:
    # missing or not a uuid -> 401: the request didn't come through the gateway
    try:
        return uuid.UUID(x_user_id)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated") from exc
