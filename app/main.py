from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    from app.core.router import router as core_router

    app.include_router(core_router)

    return app


app = create_app()
