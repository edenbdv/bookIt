from fastapi import FastAPI

from app.core.cache.rate_limit_middleware import RateLimitMiddleware
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    # BOOK-42: per-IP fixed-window limiting for signup/refresh/properties --
    # see docs/cache/rate-limiting-basics.md. /auth/login is handled
    # separately, in its own route (sliding window, per-IP + per-email).
    app.add_middleware(RateLimitMiddleware)

    # (BOOK-67: CORS is the gateway's job now -- docker-compose.yml, the `cors`
    # middleware. Adding it here too would send the header twice, which browsers reject.)

    from app.auth.router import router as auth_router
    from app.core.router import router as core_router
    from app.property.router import router as property_router
    from app.reservation.internal_router import router as reservation_internal_router
    from app.reservation.router import router as reservation_router
    from app.review.router import router as review_router

    # (BOOK-65: /payments is served by Payment's own app, services/payment/src/payment/main.py)
    app.include_router(auth_router)
    app.include_router(core_router)
    app.include_router(property_router)
    app.include_router(reservation_router)
    app.include_router(reservation_internal_router)
    app.include_router(review_router)

    return app


app = create_app()
