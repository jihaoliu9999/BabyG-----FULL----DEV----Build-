"""FastAPI application factory.

Wires marketing, auth, onboarding, and the three role consoles. Also
installs friendly 404 / 500 error pages — by default FastAPI returns
JSON, which is wrong for an HTML-rendered app.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.core.templating import templates
from app.routes import abuse as abuse_routes
from app.routes import auth as auth_routes
from app.routes import brand as brand_routes
from app.routes import creator as creator_routes
from app.routes import marketing as marketing_routes
from app.routes import onboarding as onboarding_routes
from app.routes import operator as operator_routes

logger = logging.getLogger(__name__)
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
    app.include_router(abuse_routes.router)

    @app.get("/healthz", tags=["system"])
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "env": settings.env})

    # ----- HTML error pages -----
    # Routes that explicitly raise HTTPException(401/403/404/etc.) keep
    # whatever they do today; this handler only kicks in for 404 (unknown
    # route) and unhandled 500s, where the default JSON response is wrong
    # for our HTML-rendered surfaces. Auth and authz exceptions skip the
    # template too — we don't want to render a "you can't see this" page
    # to anonymous users; better a clean status code.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> Response:
        if exc.status_code == 404:
            return templates.TemplateResponse(
                request,
                "error.html",
                {
                    "status_code": 404,
                    "title": "Page not found.",
                    "message": "We couldn't find that page. Try heading home.",
                },
                status_code=404,
            )
        # Pass through 401, 403, 422, etc. with their default JSON.
        return JSONResponse(
            {"detail": exc.detail}, status_code=exc.status_code
        )

    @app.exception_handler(Exception)
    async def _unhandled_exception_handler(
        request: Request, exc: Exception
    ) -> Response:
        # FastAPI re-raises FastAPIHTTPException through the StarletteHTTPException
        # handler above; this catches anything else (DB failures, code bugs).
        logger.exception("unhandled exception on %s", request.url.path)
        return templates.TemplateResponse(
            request,
            "error.html",
            {
                "status_code": 500,
                "title": "Something broke.",
                "message": (
                    "An error happened on our side. Try again in a minute — "
                    "if it keeps happening, ping the operator team."
                ),
            },
            status_code=500,
        )

    return app


app = create_app()
