from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.db import get_engine
from app.core.redis_client import get_redis_client

router = APIRouter(tags=["core"])


@router.get("/")
async def root() -> dict[str, str]:
    return {"service": "bookit"}


@router.get("/health")
async def health() -> JSONResponse:
    checks: dict[str, str] = {}

    # check db connection
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["database"] = f"unreachable ({type(exc).__name__})"

    # check redis connection
    try:
        await get_redis_client().ping()
        checks["redis"] = "ok"
    except Exception as exc:  # noqa: BLE001
        checks["redis"] = f"unreachable ({type(exc).__name__})"

    healthy = all(status == "ok" for status in checks.values())
    return JSONResponse(status_code=200 if healthy else 503, content=checks)
