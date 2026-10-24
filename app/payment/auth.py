"""BOOK-65: who is paying -- read from the guest's access token, without
Booking's users table. The token is verified locally (signature + exp, with
SECRET_KEY); no call to Booking. Temporary: the gateway takes this over in
BOOK-67, and Payment stops holding the user-login secret
(microservices-decisions.md §19).
"""

import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import get_settings

bearer_scheme = HTTPBearer()


async def get_guest_id(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
) -> uuid.UUID:
    # one generic 401 for every failure -- same as Booking's get_current_user
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )
    settings = get_settings()
    try:
        payload = jwt.decode(
            credentials.credentials, settings.secret_key, algorithms=[settings.jwt_algorithm]
        )
    except jwt.PyJWTError as exc:  # bad signature, expired, malformed
        raise unauthorized from exc
    # not a refresh token, not a service token
    if payload.get("type") != "access":
        raise unauthorized
    # the user id only -- unlike get_current_user there's no users-table lookup,
    # so a just-deleted user's token works until it expires (accepted, §19)
    return uuid.UUID(payload["sub"])
