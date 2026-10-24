"""BOOK-65: Payment's own HTTP app -- POST /payments and the provider's webhook,
on its own port, apart from Booking's app. Run with:

    uv run uvicorn payment.main:app --port 8001
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import text

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

    # (BOOK-67: CORS is the gateway's job -- not added here)
    app.include_router(payment_router)
    app.add_api_route("/health", health, methods=["GET"], tags=["core"])
    return app


app = create_payment_app()
