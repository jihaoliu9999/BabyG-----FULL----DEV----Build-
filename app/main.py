"""FastAPI application factory.

Wires marketing, auth, onboarding, and the three role consoles.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.routes import auth as auth_routes
from app.routes import brand as brand_routes
from app.routes import creator as creator_routes
from app.routes import marketing as marketing_routes
from app.routes import onboarding as onboarding_routes
from app.routes import operator as operator_routes

STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="babyg",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    app.include_router(marketing_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(onboarding_routes.router)
    app.include_router(creator_routes.router)
    app.include_router(brand_routes.router)
    app.include_router(operator_routes.router)

    @app.get("/healthz", tags=["system"])
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "env": settings.env})

    return app


app = create_app()
