from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.cache.rate_limit_middleware import RateLimitMiddleware
from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    # BOOK-42: per-IP fixed-window limiting for signup/refresh/properties --
    # see docs/cache/rate-limiting-basics.md. /auth/login is handled
    # separately, in its own route (sliding window, per-IP + per-email).
    app.add_middleware(RateLimitMiddleware)

    # dev-only: lets the local React dev server (a different origin/port,
    # Vite's default 5173) call this API from the browser. Tightened to
    # explicit origins, not "*", even though it's dev-only -- "*" combined
    # with allow_credentials would be rejected by browsers anyway, and being
    # explicit costs nothing here.
    if settings.environment == "development":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    from app.auth.router import router as auth_router
    from app.core.router import router as core_router
    from app.payment.router import router as payment_router
    from app.property.router import router as property_router
    from app.reservation.internal_router import router as reservation_internal_router
    from app.reservation.router import router as reservation_router
    from app.review.router import router as review_router

    app.include_router(auth_router)
    app.include_router(core_router)
    app.include_router(payment_router)
    app.include_router(property_router)
    app.include_router(reservation_router)
    app.include_router(reservation_internal_router)
    app.include_router(review_router)

    return app


app = create_app()
