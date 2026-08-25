from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

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
    from app.property.router import router as property_router
    from app.reservation.router import router as reservation_router
    from app.review.router import router as review_router

    app.include_router(auth_router)
    app.include_router(core_router)
    app.include_router(property_router)
    app.include_router(reservation_router)
    app.include_router(review_router)

    return app


app = create_app()
