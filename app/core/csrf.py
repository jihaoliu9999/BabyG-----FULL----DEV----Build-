"""CSRF protection.

We rely on a *double-submit* token signed with `session_secret`:

  * `csrf_token(request)` mints (or reuses) a token bound to the current
    session cookie. Templates render it as a hidden input named `csrf_token`
    on every state-changing form.
  * `CSRFMiddleware` rejects any unsafe-method request whose body token
    doesn't match (or whose origin/referer is foreign). Read methods (GET,
    HEAD, OPTIONS) pass through.

The token's payload is `(session_user_id, nonce)` so a stolen token from
another user can't be replayed against ours, and so logging out invalidates
all outstanding tokens. For unauthenticated POSTs (the magic-link form)
the token binds to a per-request anonymous cookie so the form is still
protected against cross-site forgery.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from urllib.parse import parse_qsl, urlparse

from fastapi import Request
from itsdangerous import BadSignature, URLSafeSerializer
from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.core.security import SESSION_COOKIE, read_session

logger = logging.getLogger(__name__)

CSRF_FIELD = "csrf_token"
CSRF_COOKIE = "bg_csrf"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Cap the body we'll buffer for token extraction. Larger uploads are
# rejected with 413 before any per-route limit gets a chance to fire,
# preventing a malicious POST from forcing the worker to allocate
# unbounded memory on the CSRF path.
MAX_CSRF_BODY_BYTES = 2 * 1024 * 1024  # 2 MiB

# Routes that legitimately accept POSTs without a CSRF token. Keep this list
# tiny and deliberate. /auth/callback is reachable from the email link in a
# fresh tab with no session cookie; everything else requires a token.
CSRF_EXEMPT_PATHS = frozenset({
    "/auth/callback",
})


def _serializer() -> URLSafeSerializer:
    return URLSafeSerializer(get_settings().session_secret, salt="bg.csrf.v1")


def _principal(request: Request) -> str:
    session = read_session(request)
    if session is not None:
        return f"u:{session['user_id']}"
    # Prefer a value we just minted for *this* response (so the token we
    # hand back is verifiable on the next POST that carries the cookie we
    # are about to set). Fall back to the existing cookie.
    anon = (
        getattr(request.state, "csrf_anon_value", None)
        or request.cookies.get(CSRF_COOKIE)
    )
    return f"a:{anon or 'none'}"


def csrf_token(request: Request) -> str:
    """Mint or reuse a CSRF token for this request.

    Stash the token on `request.state` so multiple template renders within
    one request return the same value. For anonymous requests with no
    existing CSRF cookie, also pre-allocate the cookie value here so the
    token's principal matches what the next request will see.
    """
    if hasattr(request.state, "csrf_token"):
        return request.state.csrf_token
    if read_session(request) is None and not request.cookies.get(CSRF_COOKIE):
        request.state.csrf_anon_value = secrets.token_urlsafe(16)
    nonce = secrets.token_urlsafe(16)
    token = _serializer().dumps({"p": _principal(request), "n": nonce})
    request.state.csrf_token = token
    request.state.csrf_token_minted = True
    return token


def _verify(request: Request, submitted: str) -> bool:
    if not submitted:
        return False
    try:
        data = _serializer().loads(submitted)
    except BadSignature:
        return False
    if not isinstance(data, dict):
        return False
    return hmac.compare_digest(data.get("p", ""), _principal(request))


def _origin_ok(request: Request) -> bool:
    """Reject cross-origin POSTs.

    Modern browsers (Chrome 76+, Firefox 90+, Safari 12+) always send an
    Origin header on state-changing requests. Treating a missing header
    as suspicious is the right posture: legitimate non-browser clients
    won't have a session cookie either, so they can't be CSRF vehicles.
    """
    expected = urlparse(get_settings().app_url).netloc
    for header in ("origin", "referer"):
        raw = request.headers.get(header)
        if not raw:
            continue
        host = urlparse(raw).netloc
        if not host:
            continue
        # Header set: must match the app's host or the request's host.
        return host == expected or host == request.url.netloc
    # Neither header present on a state-changing request.
    return False


class CSRFMiddleware:
    """Raw ASGI middleware that buffers the request body so we can both
    inspect it (for the CSRF token field) and replay it to the downstream
    app. `BaseHTTPMiddleware` consumes the body and FastAPI's `Form(...)`
    deps then read empty, which is why we don't subclass it here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "GET")
        path = scope.get("path", "")
        request = Request(scope)

        if method in SAFE_METHODS or path in CSRF_EXEMPT_PATHS:
            await self.app(scope, receive, _wrap_send(send, request))
            return

        if not _origin_ok(request):
            logger.warning("CSRF: cross-origin %s rejected on %s", method, path)
            await _reject(request, status_code=403, detail="csrf failed")(
                scope, receive, send
            )
            return

        body, replay, too_large = await _buffer_body(
            receive, max_bytes=MAX_CSRF_BODY_BYTES
        )
        if too_large:
            logger.warning(
                "CSRF: request body exceeded %d bytes on %s",
                MAX_CSRF_BODY_BYTES,
                path,
            )
            await _reject(request, status_code=413, detail="payload too large")(
                scope, receive, send
            )
            return

        submitted = (
            _extract_token_from_body(body, request.headers)
            or request.headers.get("x-csrf-token", "")
        )
        if not _verify(request, submitted):
            logger.warning("CSRF: token verification failed on %s", path)
            await _reject(request, status_code=403, detail="csrf failed")(
                scope, receive, send
            )
            return

        await self.app(scope, replay, _wrap_send(send, request))


def _wrap_send(send: Send, request: Request) -> Send:
    """Wrap `send` so we can append the anon CSRF cookie on the response."""

    async def _send(message: Message) -> None:
        if message["type"] == "http.response.start":
            anon = getattr(request.state, "csrf_anon_value", None)
            if anon and not request.cookies.get(SESSION_COOKIE) and not request.cookies.get(
                CSRF_COOKIE
            ):
                settings = get_settings()
                # 24h. The cookie binds nothing privileged (the
                # principal is purely anti-CSRF), so the longer
                # lifetime just prevents legitimate slow form-fillers
                # from hitting a 403 when they hit Submit after lunch.
                cookie = (
                    f"{CSRF_COOKIE}={anon}; Max-Age={60 * 60 * 24}; Path=/; HttpOnly; "
                    f"SameSite=Lax{'; Secure' if settings.is_production else ''}"
                )
                headers = MutableHeaders(raw=message.setdefault("headers", []))
                headers.append("set-cookie", cookie)
        await send(message)

    return _send


async def _empty_replay() -> Message:
    return {"type": "http.disconnect"}


async def _buffer_body(
    receive: Receive, *, max_bytes: int
) -> tuple[bytes, Receive, bool]:
    """Read the request body up to `max_bytes`, return `(body, replay,
    too_large)`. `too_large=True` means the cap was hit; in that case
    the buffered body and replay are empty — the caller MUST 413 and
    not call into the app, since downstream `Form(...)` would now read
    truncated bytes."""
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message["type"] != "http.request":
            # Disconnect or unexpected — short-circuit with an empty replay.
            return b"".join(chunks), _empty_replay, False
        chunk = message.get("body", b"")
        total += len(chunk)
        if total > max_bytes:
            return b"", _empty_replay, True
        chunks.append(chunk)
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    sent = False

    async def _replay() -> Message:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return body, _replay, False


def _reject(request: Request, *, status_code: int, detail: str) -> Response:
    """Build a rejection response. Negotiates HTML vs JSON via the
    request's Accept header so users hitting a state-changing form get
    a readable page instead of a raw `{"detail": ...}`."""
    accept = (request.headers.get("accept") or "").lower()
    if "text/html" in accept or ("*/*" in accept and "json" not in accept):
        body = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>Request blocked</title>"
            "<h1>Request blocked</h1>"
            f"<p>{detail}.</p>"
            "<p><a href=\"/\">Go home</a></p>"
        )
        return HTMLResponse(body, status_code=status_code)
    return JSONResponse({"detail": detail}, status_code=status_code)


def _extract_token_from_body(body: bytes, headers: Headers) -> str:
    content_type = (headers.get("content-type") or "").split(";")[0].strip().lower()
    if content_type == "application/x-www-form-urlencoded":
        try:
            decoded = body.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return ""
        for key, value in parse_qsl(decoded, keep_blank_values=True):
            if key == CSRF_FIELD:
                return value
        return ""
    # multipart/form-data — minimal parse: scan for the field name. This is
    # narrow but every multipart form we generate carries a single short
    # `csrf_token` field rendered first via the include partial.
    if content_type == "multipart/form-data":
        marker = (
            b'name="' + CSRF_FIELD.encode() + b'"\r\n\r\n'
        )
        idx = body.find(marker)
        if idx == -1:
            return ""
        start = idx + len(marker)
        end = body.find(b"\r\n", start)
        if end == -1:
            return ""
        try:
            return body[start:end].decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return ""
    return ""


def init_csrf_for_templates(templates) -> None:
    """Expose `csrf_token` to every template via the Jinja env globals.

    Templates render `{{ csrf_token(request) }}` inside a hidden input on
    every state-changing form. We pass `request` explicitly because
    Starlette's TemplateResponse populates it but Jinja can't pull it from
    thin air across includes.
    """
    templates.env.globals["csrf_token"] = csrf_token
