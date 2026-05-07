"""FastAPI application factory.

Wires marketing, auth, onboarding, creator dashboard, and operator console.
The /brand surface is still a stub here until the brand console ships.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.core.security import SessionPayload
from app.deps import require_role
from app.routes import auth as auth_routes
from app.routes import creator as creator_routes
from app.routes import marketing as marketing_routes
from app.routes import onboarding as onboarding_routes
from app.routes import operator as operator_routes
from app.services import profiles

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
    app.include_router(operator_routes.router)

    @app.get("/brand", response_class=HTMLResponse, tags=["brand"])
    async def brand_home(
        request: Request, session: SessionPayload = Depends(require_role("brand"))
    ) -> Response:
        if not profiles.is_brand_onboarded(session["user_id"]):
            return RedirectResponse("/onboarding/brand", status_code=302)
        return _stub(request, role="brand", title="Brand console")

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
      <p class='auth-sub'>Signed in and onboarded. Brand console ships in a later step.</p>
      <form method='post' action='/auth/logout'>
        <button class='btn btn-ghost' type='submit'>Sign out</button>
      </form>
    </section></main></body></html>
    """
    return HTMLResponse(body)


app = create_app()
