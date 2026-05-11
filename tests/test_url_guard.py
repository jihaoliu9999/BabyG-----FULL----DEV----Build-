"""Tests for `http_url_or_none` — the XSS-via-href defense.

User-supplied URLs that get rendered into Jinja `href="{{ value }}"`
must be funnelled through this gate first. Anything that isn't an
http(s) URL with a host is rejected. The regression we're locking in
is the `javascript:` URL scheme — Jinja's autoescape would NOT block
it, so a malicious brand could ship a stored XSS payload by setting
their website to `javascript:fetch('/exfil?'+document.cookie)`.
"""

from __future__ import annotations

import pytest

from app.core.url_guard import http_url_or_none


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://example.com",
        "https://sub.example.com/path?q=1",
        "  https://example.com  ",   # strip whitespace
        "HTTP://EXAMPLE.COM",         # case-insensitive scheme
    ],
)
def test_accepts_valid_http_urls(value):
    out = http_url_or_none(value)
    assert out is not None
    # Returned value preserves the original (less whitespace) — it's the
    # validator, not a normalizer.
    assert out.lower().startswith(("http://", "https://"))


@pytest.mark.parametrize(
    "value",
    [
        "javascript:fetch('/exfil?'+document.cookie)",
        "JAVASCRIPT:alert(1)",
        "javascript:void(0)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "mailto:someone@example.com",
        "tel:+15551234567",
        "ftp://example.com",
        "//example.com/protocol-relative",
        "example.com",                   # no scheme
        "/relative/path",
        "?query=only",
        "#fragment-only",
        "",
        "   ",
        "http://",                       # scheme but no host
        "https:///path-only",
        None,
    ],
)
def test_rejects_non_http_or_malformed(value):
    assert http_url_or_none(value) is None


def test_rejects_non_string_input():
    for v in (123, ["http://example.com"], object(), b"http://example.com"):
        assert http_url_or_none(v) is None  # type: ignore[arg-type]
