"""Defense for user-supplied URLs that get rendered into HTML `href` attrs.

Several user-submitted fields (`brand_website`, `intel.source`,
`receipt.post_url`) flow into `<a href="{{ value }}">`. Jinja's
autoescape protects against attribute-quote escapes but does NOT block
the `javascript:` URL scheme: a malicious value like
`javascript:fetch('/exfil?'+document.cookie)` would execute in the
clicker's session context. Funnel any such value through
`http_url_or_none` first.
"""

from __future__ import annotations

from urllib.parse import urlparse

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def http_url_or_none(value: str | None) -> str | None:
    """Return `value` if it's a syntactically valid http(s) URL with a
    host, otherwise None.

    Accepts:
      * `http://example.com/path`
      * `https://sub.example.com`
    Rejects everything else, including:
      * `javascript:fetch(...)`         — explicit attack vector
      * `data:text/html,...`            — would execute in some renderers
      * `//example.com/path`            — protocol-relative
      * `example.com`                   — missing scheme
      * `mailto:`, `tel:`, etc.         — out of scope for these fields

    Callers should treat a None return as a validation error and surface
    a "please enter a valid http(s) URL" message to the user.
    """
    if not value or not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.scheme.lower() not in _ALLOWED_SCHEMES:
        return None
    if not parsed.netloc:
        return None
    return value
