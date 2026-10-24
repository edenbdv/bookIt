"""BOOK-65: Payment's own HTTP app -- POST /payments and the provider's webhook,
on its own port, apart from Booking's app. Run with:

    uv run uvicorn payment.main:app --port 8001
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from payment.config import get_payment_settings
from payment.db import get_payment_engine
from payment.router import router as payment_router


async def health() -> JSONResponse:
    # Payment's own database is its only hard dependency -- Booking being down
    # makes POST /payments answer 503, but Payment itself is still up
    try:
        async with get_payment_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks = {"database": "ok"}
    except Exception as exc:  # noqa: BLE001
        checks = {"database": f"unreachable ({type(exc).__name__})"}
    healthy = checks["database"] == "ok"
    return JSONResponse(status_code=200 if healthy else 503, content=checks)


def create_payment_app() -> FastAPI:
    app = FastAPI(title="BookIt Payment")

    # dev-only, same as Booking's app: the local frontend calls Payment directly
    # until the gateway sits in front of both (BOOK-67)
    if get_payment_settings().environment == "development":
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

    app.include_router(payment_router)
    app.add_api_route("/health", health, methods=["GET"], tags=["core"])
    return app


app = create_payment_app()
