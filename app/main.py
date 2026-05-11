"""FastAPI application factory.

Wires marketing, auth, onboarding, and the three role consoles. Also
installs friendly 404 / 500 error pages — by default FastAPI returns
JSON, which is wrong for an HTML-rendered app.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

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
    _assert_session_secret(settings)
    _configure_logging(settings)
    app = FastAPI(
        title="babyg",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    # GZip every response over 1KB. Most HTML pages are 5-30KB → ~70% off the wire.
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    # CSRF check on every unsafe-method request. Templates render the token
    # via `{{ csrf_token(request) }}`; the middleware verifies it.
    from app.core.csrf import CSRFMiddleware
    app.add_middleware(CSRFMiddleware)

    # Conservative security headers. CSP intentionally inline-style permissive
    # because the templates use a few inline `style=` attributes; tighten later.
    app.add_middleware(_SecurityHeadersMiddleware)

    app.mount("/static", _CachedStatic(directory=str(STATIC_DIR)), name="static")

    app.include_router(marketing_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(onboarding_routes.router)
    app.include_router(creator_routes.router)
    app.include_router(brand_routes.router)
    app.include_router(operator_routes.router)
    app.include_router(abuse_routes.router)

    @app.get("/healthz", tags=["system"])
    async def healthz() -> JSONResponse:
        # Don't leak environment metadata. Restart logic intentionally
        # doesn't depend on Supabase; that's a deliberate choice (see DEPLOY.md).
        return JSONResponse({"status": "ok"})

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> Response:
        # Invite-only. Don't index anything.
        return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

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
        # Anonymous users hitting an HTML role-gated route used to get raw
        # JSON {"detail": "auth required"} — a dead-end. Redirect them to
        # the role-aware login. POSTs and JSON consumers (Accept: */* without
        # text/html, or anything under /report) keep the old JSON behavior.
        if exc.status_code == 401 and _wants_html(request):
            role = _role_hint_from_path(request.url.path)
            return RedirectResponse(f"/auth/login?role={role}", status_code=302)
        # Pass through 403, 422, etc. with their default JSON.
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
        try:
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
        except Exception:
            # Templating itself failed. Don't compound the error with an
            # empty body — surface a plain-text 500 so monitoring still
            # sees the right status code.
            logger.exception("error.html render also failed")
            return Response(
                "Internal server error.",
                status_code=500,
                media_type="text/plain",
            )

    return app


class _SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        # HSTS only matters when served over HTTPS; safe to send regardless.
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
        # Minimal CSP. We don't load any external scripts, so default-src 'self'
        # is enough; allow inline `style=` attributes that the templates use.
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none';",
        )
        return response


class _CachedStatic(StaticFiles):
    """StaticFiles with a 1-hour public Cache-Control on every asset."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers.setdefault(
                "Cache-Control", "public, max-age=3600, immutable"
            )
        return response


DEFAULT_DEV_SECRET = "dev-only-not-secure-replace-me"


def _configure_logging(settings) -> None:
    """Wire INFO-level stream logging.

    Without this, gunicorn's default WARNING level swallows every
    `logger.exception(...)` call in services — service errors disappear
    silently in production. Format includes the level + module + path
    so structured ingest tools can grep on it later.
    """
    if logging.getLogger().handlers:
        # gunicorn / pytest may have already configured root; don't fight.
        return
    logging.basicConfig(
        level=logging.INFO if settings.env != "dev" else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _assert_session_secret(settings) -> None:
    """Refuse to boot in production or staging with a weak/default secret.

    `URLSafeTimedSerializer` accepts an empty string and silently signs
    with it, which would let any visitor mint sessions. Enforce a
    non-default, ≥32-char value for every internet-reachable env.
    Local `dev` is exempt so contributors can run without an .env.
    """
    secret = settings.session_secret or ""
    if settings.env == "dev":
        return
    if not secret or secret == DEFAULT_DEV_SECRET:
        raise RuntimeError(
            f"SESSION_SECRET is unset or still the dev default in env={settings.env}. "
            "Generate one with: python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    if len(secret) < 32:
        raise RuntimeError(
            f"SESSION_SECRET must be at least 32 characters in env={settings.env}."
        )


def _wants_html(request: Request) -> bool:
    if request.method != "GET":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or accept == "" or accept == "*/*"


def _role_hint_from_path(path: str) -> str:
    if path.startswith("/brand"):
        return "brand"
    if path.startswith("/operator"):
        return "operator"
    return "creator"


app = create_app()
