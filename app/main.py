"""FastAPI application factory.

Wires marketing, auth, onboarding, and the three role consoles. Also
installs friendly 404 / 500 error pages — by default FastAPI returns
JSON, which is wrong for an HTML-rendered app.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from app.config import get_settings
from app.core.templating import templates
from app.routes import abuse as abuse_routes
from app.routes import auth as auth_routes
from app.routes import brand as brand_routes
from app.routes import creator as creator_routes
from app.routes import discover as discover_routes
from app.routes import legal as legal_routes
from app.routes import marketing as marketing_routes
from app.routes import onboarding as onboarding_routes
from app.routes import operator as operator_routes

logger = logging.getLogger(__name__)
STATIC_DIR = Path(__file__).resolve().parent / "static"


def create_app() -> FastAPI:
    settings = get_settings()
    _assert_session_secret(settings)
    _assert_app_url(settings)
    _configure_logging(settings)
    # Surface migration drift between repo files and the Supabase
    # registry. Logs WARN on drift by default; fails the boot when
    # STRICT_MIGRATION_CHECK=1. Skipped in env=dev. Never crashes on
    # transient Supabase errors — see app/core/migration_check.py.
    from app.core.migration_check import assert_migrations_applied

    assert_migrations_applied(settings)
    app = FastAPI(
        title="babyg",
        version="0.1.0",
        docs_url="/docs" if not settings.is_production else None,
        redoc_url=None,
    )

    # Railway terminates TLS before forwarding to the app. Teach Starlette's
    # URL generation about the original scheme so `url_for(...)` emits HTTPS.
    app.add_middleware(_ForwardedProtoMiddleware)

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
    app.include_router(discover_routes.router)
    app.include_router(brand_routes.router)
    app.include_router(operator_routes.router)
    app.include_router(abuse_routes.router)
    app.include_router(legal_routes.router)

    @app.get("/healthz", tags=["system"])
    async def healthz() -> JSONResponse:
        # Don't leak environment metadata. Restart logic intentionally
        # doesn't depend on Supabase; that's a deliberate choice (see DEPLOY.md).
        return JSONResponse({"status": "ok"})

    @app.get("/robots.txt", include_in_schema=False)
    async def robots() -> Response:
        # Invite-only. Don't index anything.
        return Response("User-agent: *\nDisallow: /\n", media_type="text/plain")

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon_ico() -> FileResponse:
        # Browsers auto-request /favicon.ico even when the HTML links to
        # a static path. Serve the same file so those hits don't 404.
        return FileResponse(
            STATIC_DIR / "assets" / "favicon.ico",
            media_type="image/x-icon",
            headers={"Cache-Control": "public, max-age=3600"},
        )

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


class _ForwardedProtoMiddleware:
    """Apply trusted proxy scheme from Railway's X-Forwarded-Proto header."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"}:
            proto = Headers(scope=scope).get("x-forwarded-proto", "")
            scheme = proto.split(",", 1)[0].strip().lower()
            if scheme in {"http", "https"}:
                scope = dict(scope)
                scope["scheme"] = scheme
        await self.app(scope, receive, send)


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
        # Minimal CSP. We don't load external scripts or stylesheets. Linked
        # CSS is same-origin only; inline style attributes are allowed because
        # a few operator templates still use `style=`.
        # img-src includes the Supabase Storage origin so creator profile
        # photos (hosted in the `profile-photos` public bucket) render.
        # form-action allow-lists accounts.google.com because the Google OAuth
        # picker POSTs to a same-origin handler that 302s to Google — browsers
        # enforce form-action across the entire navigation chain, so the
        # redirect target must be allow-listed too or the redirect is blocked.
        settings = get_settings()
        img_src = "img-src 'self' data:"
        supabase_origin = _origin(settings.supabase_url)
        if supabase_origin:
            img_src = f"{img_src} {supabase_origin}"
        csp = (
            f"default-src 'self'; {img_src}; "
            "script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "style-src-elem 'self'; "
            # connect-src allow-lists the BigDataCloud free reverse-geocode
            # endpoint so the profile + onboarding location flows can turn
            # browser coords into a city/region/country label without an
            # API key. The endpoint is read-only and cors-friendly; nothing
            # else (auth, app secret) is sent.
            "connect-src 'self' https://api.bigdatacloud.net; "
            "form-action 'self' https://accounts.google.com; "
            "frame-ancestors 'none';"
        )
        if settings.env != "dev":
            csp = f"{csp} upgrade-insecure-requests;"
        response.headers.setdefault(
            "Content-Security-Policy",
            csp,
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


def _assert_app_url(settings) -> None:
    """Refuse to boot in staging/production with a misconfigured APP_URL.

    Magic-link auth builds the callback as `{APP_URL}/auth/callback` and
    Supabase will only accept the redirect if its allow-list matches
    exactly. A wrong scheme, a localhost leak, or an empty value breaks
    every signup silently — users click the link and land on a
    redirect_uri error from Supabase, not on babyg. Catch it at boot.
    Local `dev` keeps the http://localhost default.
    """
    from urllib.parse import urlsplit

    url = (settings.app_url or "").strip()
    if settings.env == "dev":
        return
    if not url:
        raise RuntimeError(
            f"APP_URL is unset in env={settings.env}. "
            "Set it to the canonical https origin Supabase Auth is configured to redirect to."
        )
    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise RuntimeError(f"APP_URL is not a valid URL in env={settings.env}: {exc}") from exc
    if parts.scheme != "https":
        raise RuntimeError(
            f"APP_URL must use https in env={settings.env}, got scheme={parts.scheme!r}. "
            "Magic-link callbacks fail without TLS."
        )
    host = (parts.hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError(
            f"APP_URL points at {host!r} in env={settings.env}. "
            "Use the public hostname Supabase Auth is configured to redirect to."
        )


def _origin(url: str) -> str:
    """Return scheme://host[:port] from a URL, or "" if unparseable."""
    if not url:
        return ""
    try:
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if not parts.scheme or not parts.netloc:
            return ""
        return f"{parts.scheme}://{parts.netloc}"
    except ValueError:
        return ""


def _wants_html(request: Request) -> bool:
    if request.method != "GET":
        return False
    accept = request.headers.get("accept", "")
    return "text/html" in accept or accept == "" or accept == "*/*"


def _role_hint_from_path(path: str) -> str:
    if path.startswith("/operator") or path.startswith("/onboarding/operator"):
        return "operator"
    if path.startswith("/brand") or path.startswith("/onboarding/brand"):
        return "brand"
    return "creator"


app = create_app()
