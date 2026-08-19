from fastapi import FastAPI

from app.core.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name)

    from app.core.router import router as core_router
    from app.property.router import router as property_router

    app.include_router(core_router)
    app.include_router(property_router)

    return app


app = create_app()
