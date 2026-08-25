"""Per-request timing middleware.

The audit found no route-level timing anywhere in the request path, so
we couldn't tell a slow Anthropic call from a slow Supabase query from
Railway cold-start. This is the smallest safe fix: an ASGI middleware
that wall-times every HTTP request, appends an ``X-Response-Time``
header, and emits ONE INFO log line per request.

Only static-asset paths are skipped — they'd flood the log with
30 ms lines that don't reflect any dynamic work. Everything else
(HTML routes, API routes, healthcheck, favicon, robots, sitemap) is
timed.

Privacy: only the URL path is logged, never the query string, request
body, cookies, headers, or response body. That keeps DMs, tokens, and
OAuth codes out of the log stream.
"""

from __future__ import annotations

import logging
import time

from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("babyg.request_timing")

# Static assets are served by StaticFiles at /static/... — they don't
# reflect any dynamic work and would drown the log stream. Skip them.
_SKIP_PATH_PREFIXES: tuple[str, ...] = ("/static/",)


class RequestTimingMiddleware:
    """ASGI middleware that logs `method path status ms` per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path: str = scope.get("path", "") or ""
        if any(path.startswith(prefix) for prefix in _SKIP_PATH_PREFIXES):
            await self.app(scope, receive, send)
            return

        method: str = scope.get("method", "") or ""
        start = time.perf_counter()
        status_holder: dict[str, int] = {"code": 0}

        wrapped_send = _make_wrapped_send(send, start, status_holder)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            duration_ms = (time.perf_counter() - start) * 1000.0
            status = status_holder["code"] or 0
            # One concise line — greppable, no PII, no query string.
            logger.info(
                "method=%s path=%s status=%d duration_ms=%.1f",
                method,
                path,
                status,
                duration_ms,
            )


def _make_wrapped_send(
    send: Send, start: float, status_holder: dict[str, int]
) -> Send:
    """Return a send callable that injects X-Response-Time on response start.

    We do it here (not in the outer try/finally) so the header is set
    before the response body starts streaming — otherwise Starlette
    would already have flushed headers.
    """

    async def _send(message: Message) -> None:
        if message["type"] == "http.response.start":
            elapsed_ms = (time.perf_counter() - start) * 1000.0
            status_holder["code"] = int(message.get("status", 0) or 0)
            # ASGI headers are (bytes, bytes) tuples; make a fresh list
            # so we don't mutate any object other frameworks might hold.
            headers = list(message.get("headers") or [])
            headers.append(
                (b"x-response-time", f"{elapsed_ms:.1f}ms".encode("latin-1"))
            )
            message = {**message, "headers": headers}
        await send(message)

    return _send
