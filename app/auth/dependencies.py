import uuid

from fastapi import Depends, HTTPException, Request, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.models import User
from app.auth.security import TokenError, decode_token
from app.core.cache.rate_limit import check_fixed_window
from app.core.config import get_settings
from app.core.db.unit_of_work import UnitOfWork, get_uow

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


async def enforce_user_rate_limit(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user),
) -> None:
    """Per-user_id limit + a coarser per-IP floor, for routes behind get_current_user.

    FastAPI caches get_current_user's result per request, so declaring it
    here too doesn't decode/look up the token twice (dependency-injection-basics.md).

    Fails OPEN on a Redis error -- the opposite of login's fail-closed.
    This limiter exists for fairness/load-shaping between users, not as a
    security boundary; blocking every authenticated action in the app
    because Redis hiccuped is worse than briefly losing that fairness
    check. See the "Decision" table in docs/cache/rate-limiting-basics.md.

    Unlike RateLimitMiddleware, there's no call_next() here to attach
    X-RateLimit-* to after the fact on the success path -- FastAPI merges
    headers set on the injected `response` into the real one, which is the
    dependency-layer equivalent. Only the per-user check's numbers are
    exposed; the per-IP floor is a silent backstop, not something a client
    tunes against.
    """
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"

    user_key = f"ratelimit:user:{current_user.id}"
    try:
        user_result = await check_fixed_window(
            user_key, settings.rate_limit_user_limit, settings.rate_limit_user_window_seconds
        )
    except Exception:  # noqa: BLE001
        user_result = None  # fail open -- see docstring above

    if user_result is not None:
        response.headers["X-RateLimit-Limit"] = str(user_result.limit)
        response.headers["X-RateLimit-Remaining"] = str(user_result.remaining_requests)
        if not user_result.allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="Too many requests",
                headers={"Retry-After": str(user_result.retry_after_seconds)},
            )

    floor_key = f"ratelimit:user_ip_floor:{client_ip}"
    try:
        floor_result = await check_fixed_window(
            floor_key,
            settings.rate_limit_user_ip_floor_limit,
            settings.rate_limit_user_ip_floor_window_seconds,
        )
    except Exception:  # noqa: BLE001
        return  # fail open -- see docstring above
    if not floor_result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(floor_result.retry_after_seconds)},
        )
