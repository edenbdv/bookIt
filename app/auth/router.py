from collections.abc import Awaitable

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from redis.exceptions import RedisError

from app.auth import service
from app.auth.dependencies import get_current_user
from app.auth.models import User
from app.auth.schemas import (
    LogoutRequest,
    RefreshRequest,
    Token,
    UserCreate,
    UserLogin,
    UserRead,
)
from app.core.cache.rate_limit import check_sliding_window
from app.core.config import get_settings
from app.core.db.unit_of_work import UnitOfWork, get_uow

router = APIRouter(prefix="/auth", tags=["auth"])


async def _enforce_login_rate_limit(key: str, limit: int, window_seconds: int) -> None:
    """Sliding-window check for one of login's two keys (IP or email).

    Fails closed on a Redis error: unlike signup/refresh/properties
    (RateLimitMiddleware), login IS the security control (credential
    stuffing), not just fairness/load-shaping -- see the "Decision" table
    in docs/cache/rate-limiting-basics.md. No X-RateLimit-* headers here
    either, per that doc's response-format section: exposing them on login
    specifically hands an attacker exact telemetry on how close they are
    to the threshold.
    """
    try:
        result = await check_sliding_window(key, limit, window_seconds)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from exc
    if not result.allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests",
            headers={"Retry-After": str(result.retry_after_seconds)},
        )


async def _with_token_store[T](awaitable: Awaitable[T]) -> T:
    """Run a call that touches the refresh-token store, failing CLOSED on a Redis error.

    Same call as _enforce_login_rate_limit's: this store IS the security control
    (BOOK-78 rotation/revocation), so if it's unreachable the honest answer is
    503. Failing open would silently restore exactly the pre-BOOK-78 behaviour
    the ticket exists to remove -- any signed token accepted, unlimited times.
    That's the opposite of enforce_user_rate_limit's fail-open, which protects
    fairness rather than a boundary.
    """
    try:
        return await awaitable
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Service temporarily unavailable",
            headers={"Retry-After": "5"},
        ) from exc


@router.post("/signup", response_model=UserRead, status_code=status.HTTP_201_CREATED)
async def signup(
    data: UserCreate,
    uow: UnitOfWork = Depends(get_uow),
) -> User:
    try:
        return await service.signup(uow, data)
    except service.EmailAlreadyExistsError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Email already registered"
        ) from exc


@router.post("/login", response_model=Token)
async def login(
    request: Request,
    data: UserLogin,
    uow: UnitOfWork = Depends(get_uow),
) -> Token:
    settings = get_settings()
    client_ip = request.client.host if request.client else "unknown"

    # per-IP first: cheap short-circuit, and independent of whether the
    # email exists so it can't itself leak enumeration info by ordering
    await _enforce_login_rate_limit(
        f"ratelimit:login:ip:{client_ip}",
        settings.rate_limit_login_ip_limit,
        settings.rate_limit_login_ip_window_seconds,
    )
    # per-email: counted the same way whether or not this email is
    # registered -- runs before any DB lookup, see
    # docs/auth/user-enumeration-basics.md
    await _enforce_login_rate_limit(
        f"ratelimit:login:email:{data.email}",
        settings.rate_limit_login_email_limit,
        settings.rate_limit_login_email_window_seconds,
    )

    try:
        user = await service.login(uow, data.email, data.password)
    except service.InvalidCredentialsError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid email or password"
        ) from exc
    return await _with_token_store(service.issue_tokens(user.id))


@router.post("/refresh", response_model=Token)
async def refresh(
    data: RefreshRequest,
    uow: UnitOfWork = Depends(get_uow),
) -> Token:
    try:
        return await _with_token_store(service.refresh_tokens(uow, data.refresh_token))
    except service.InvalidTokenError as exc:
        # one message for expired, forged, already-rotated and revoked alike --
        # "that token was already used" would confirm to a thief that the token
        # they hold is genuine (docs/auth/user-enumeration-basics.md's reasoning,
        # applied to tokens)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        ) from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(data: LogoutRequest) -> None:
    """End this session. Idempotent: a token that's already dead still gets 204.

    Deliberately no 401 on a bad token -- logout has nothing to protect. Failing
    it would only ever leave a client stuck holding credentials it wants to
    discard, and the generic-error reasoning on /refresh applies here too.
    """
    try:
        await _with_token_store(service.logout(data.refresh_token))
    except service.InvalidTokenError:
        return


@router.post("/logout-all", status_code=status.HTTP_204_NO_CONTENT)
async def logout_all(current_user: User = Depends(get_current_user)) -> None:
    """Sign out every device. Authenticated by access token, not refresh, so a
    user who has lost a device can do it from a session they still hold."""
    await _with_token_store(service.logout_all(current_user.id))


@router.get("/me", response_model=UserRead)
async def get_me(current_user: User = Depends(get_current_user)) -> User:
    return current_user


@router.get("/verify", status_code=status.HTTP_204_NO_CONTENT)
async def verify(response: Response, current_user: User = Depends(get_current_user)) -> None:
    """BOOK-67b: the gateway's forward auth asks this before guest /payments requests.
    Valid access token -> 204 + X-User-Id (copied onto the forwarded request);
    anything else -> get_current_user's 401."""
    response.headers["X-User-Id"] = str(current_user.id)
