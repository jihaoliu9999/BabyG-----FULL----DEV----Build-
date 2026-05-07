"""FastAPI application factory.

Phase 1 Step 3: marketing pages, magic-link auth, role-aware redirects.
The actual creator/brand/operator dashboards are stubs here and will be
filled in in Phase 1 Step 4+.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.deps import require_role
from app.routes import auth as auth_routes
from app.routes import marketing as marketing_routes

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

    @app.get("/creator", response_class=HTMLResponse, tags=["creator"])
    async def creator_home(request: Request, session=Depends(require_role("creator"))):
        return _stub(request, role="creator", title="Creator dashboard")

    @app.get("/brand", response_class=HTMLResponse, tags=["brand"])
    async def brand_home(request: Request, session=Depends(require_role("brand"))):
        return _stub(request, role="brand", title="Brand console")

    @app.get("/operator", response_class=HTMLResponse, tags=["operator"])
    async def operator_home(request: Request, session=Depends(require_role("operator"))):
        return _stub(request, role="operator", title="Operator console")

    @app.get("/healthz", tags=["system"])
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok", "env": settings.env})

    return app


def _stub(request: Request, *, role: str, title: str) -> HTMLResponse:
    css_url = request.url_for("static", path="/css/app.css")
    body = f"""
    <!doctype html><html><head><meta charset='utf-8'><title>{title} · babyg</title>
    <link rel='stylesheet' href='{css_url}'></head>
    <body class='theme-dark'><main class='page'><section class='auth-pane'>
      <div class='auth-tag'>{role.capitalize()}</div>
      <h1 class='auth-title'>{title}</h1>
      <p class='auth-sub'>Signed in. Dashboard ships in Phase 1 Step 4.</p>
      <form method='post' action='/auth/logout'>
        <button class='btn btn-ghost' type='submit'>Sign out</button>
      </form>
    </section></main></body></html>
    """
    return HTMLResponse(body)


app = create_app()
