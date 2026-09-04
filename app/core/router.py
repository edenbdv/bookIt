from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.cache.redis_client import get_redis_client
from app.core.db.engine import get_engine
from app.worker import ping

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


# BOOK-44: temporary proof that enqueue -> execute works end to end through
# Celery/RabbitMQ. Not a real feature -- delete once BOOK-45 (the real email
# job) exists and can serve as the actual end-to-end proof instead. See
# docs/jobs/celery-basics.md section 2 for what .delay() actually does.
@router.post("/jobs/ping")
async def enqueue_ping() -> dict[str, str]:
    result = ping.delay()
    return {"task_id": result.id}
