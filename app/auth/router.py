from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.auth import service
from app.auth.models import User
from app.auth.schemas import RefreshRequest, Token, UserCreate, UserLogin, UserRead
from app.core.config import get_settings
from app.core.rate_limit import check_sliding_window
from app.core.unit_of_work import UnitOfWork, get_uow

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
    return service.issue_tokens(user.id)


@router.post("/refresh", response_model=Token)
async def refresh(
    data: RefreshRequest,
    uow: UnitOfWork = Depends(get_uow),
) -> Token:
    try:
        return await service.refresh_tokens(uow, data.refresh_token)
    except service.InvalidTokenError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or expired refresh token"
        ) from exc
