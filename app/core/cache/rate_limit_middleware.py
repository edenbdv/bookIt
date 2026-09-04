import logging

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.core.cache.rate_limit import check_fixed_window
from app.core.config import get_settings

logger = logging.getLogger(__name__)

# Only the uniform, per-IP, fixed-window public routes live here -- see
# docs/cache/rate-limiting-basics.md's "Decision" table. /auth/login is
# deliberately NOT included: it's sliding-window, checked per-IP AND
# per-email, and needs the parsed body (the email), which doesn't belong
# in a generic middleware that every other route here has no need of.
_RULES: dict[tuple[str, str], tuple[str, bool]] = {
    ("POST", "/auth/signup"): ("auth", True),  # fail closed
    ("POST", "/auth/refresh"): ("auth", True),  # fail closed
}
_PROPERTIES_GROUP = ("properties", False)  # fail open


def _classify(request: Request) -> tuple[str, bool] | None:
    rule = _RULES.get((request.method, request.url.path))
    if rule is not None:
        return rule
    if request.method == "GET" and request.url.path.startswith("/properties"):
        return _PROPERTIES_GROUP
    return None


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        classification = _classify(request)
        if classification is None:
            return await call_next(request)

        group, fail_closed = classification
        settings = get_settings()
        limit: int = getattr(settings, f"rate_limit_{group}_ip_limit")
        window_seconds: int = getattr(settings, f"rate_limit_{group}_ip_window_seconds")

        client_ip = request.client.host if request.client else "unknown"
        key = f"ratelimit:{group}:ip:{client_ip}"

        try:
            result = await check_fixed_window(key, limit, window_seconds)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "rate limit check failed for %s, %s: %s",
                key,
                "fail-closed" if fail_closed else "fail-open",
                exc,
            )
            if fail_closed:
                return JSONResponse(
                    status_code=503,
                    content={"detail": "Service temporarily unavailable"},
                    headers={"Retry-After": "5"},
                )
            return await call_next(request)

        if not result.allowed:
            return JSONResponse(
                status_code=429,
                content={"detail": "Too many requests"},
                headers={
                    "Retry-After": str(result.retry_after_seconds),
                    "X-RateLimit-Limit": str(result.limit),
                    "X-RateLimit-Remaining": str(result.remaining_requests),
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(result.limit)
        response.headers["X-RateLimit-Remaining"] = str(result.remaining_requests)
        return response
