"""FastAPI application factory.

Phase 1 Step 1: bootable scaffold with /healthz only. Routers, middleware,
templates, and integrations are wired in subsequent Phase 1 steps.
"""

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="babyg",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    @app.get("/healthz", tags=["system"])
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "env": settings.env})

    return app


app = create_app()
