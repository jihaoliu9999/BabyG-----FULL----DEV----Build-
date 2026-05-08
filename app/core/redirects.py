"""Shared helpers for safely redirecting to user-supplied paths.

Any route that takes a `return_to` / `target` value out of a form or query
string and uses it in a `RedirectResponse` MUST funnel it through
`safe_same_origin` first. Otherwise the route is an open redirect — an
attacker can craft a link like /creator/notifications/<id>/read with
`target=https://evil.example` and use it for phishing.
"""

from __future__ import annotations

from urllib.parse import urlparse


def safe_same_origin(value: str | None, *, default: str = "/") -> str:
    """Return `value` if it's a same-origin path, otherwise `default`.

    A same-origin path:
      * starts with a single `/` (no `//evil.example` protocol-relative)
      * has no scheme, no netloc
    """
    if not value or not isinstance(value, str):
        return default
    if not value.startswith("/") or value.startswith("//"):
        return default
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return default
    return value
