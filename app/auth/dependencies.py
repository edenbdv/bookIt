import uuid

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import User
from app.auth.security import TokenError, decode_token
from app.core.unit_of_work import UnitOfWork, get_uow

# HTTPBearer, not OAuth2PasswordBearer -- our /auth/login takes a JSON body
# (UserLogin), not OAuth2's form-encoded grant, so HTTPBearer's plain
# "extract the Authorization: Bearer <token> header" matches our actual
# contract instead of implying a login flow we don't have
bearer_scheme = HTTPBearer()


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    uow: UnitOfWork = Depends(get_uow),
) -> User:
    # one generic 401 for every failure reason below -- same "don't leak
    # which check failed" reasoning as login (user-enumeration-basics.md)
    # and /auth/refresh
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload = decode_token(credentials.credentials)
    except TokenError as exc:
        raise unauthorized from exc

    # rejects a refresh token presented here -- the inverse of
    # refresh_tokens()'s own type check (service.py)
    if payload.get("type") != "access":
        raise unauthorized

    user = await uow.users.get_by_id(uuid.UUID(payload["sub"]))
    # covers a deleted/no-longer-existing user holding an otherwise-valid,
    # unexpired access token
    if user is None:
        raise unauthorized

    return user
