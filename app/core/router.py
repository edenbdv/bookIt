from fastapi import APIRouter

router = APIRouter(tags=["core"])


@router.get("/")
async def root() -> dict[str, str]:
    return {"service": "bookit"}
