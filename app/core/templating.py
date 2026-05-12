"""Single Jinja2Templates instance shared across all routers."""

from __future__ import annotations

from pathlib import Path

from fastapi.templating import Jinja2Templates

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


def _short_dt(value):
    """Render a Postgres ISO timestamptz as `YYYY-MM-DD HH:MM`.

    Templates were doing `m.created_at[:16]|replace("T", " ")` everywhere,
    which silently mis-renders if the driver ever returns a different
    shape. Centralizing the format here means one place to fix.
    """
    if not value:
        return ""
    s = str(value)
    return s[:16].replace("T", " ")


def _short_date(value):
    if not value:
        return ""
    return str(value)[:10]


def _safe_url(value):
    """Render-time defense: collapse non-http(s) URLs to "#".

    Validators (`app/core/url_guard`) already gate user input at write
    time, but a row could pre-date the validator, be inserted via
    Supabase Studio, or arrive from a future import path. Use this
    filter on every `href` that interpolates a stored URL:

        <a href="{{ row.url|safe_url }}">...</a>

    Anything not starting with `http://` or `https://` becomes "#",
    so a `javascript:` payload in a stale row renders as a dead link
    instead of executing.
    """
    if not value:
        return "#"
    s = str(value).strip()
    if not s:
        return "#"
    lower = s.lower()
    if lower.startswith("http://") or lower.startswith("https://"):
        return s
    return "#"


templates.env.filters["short_dt"] = _short_dt
templates.env.filters["short_date"] = _short_date
templates.env.filters["safe_url"] = _safe_url

# Lazy-import to avoid a circular: csrf.py imports from app.config which is
# safe, but app.core.security imports app.config too and we don't want any
# template-time gotchas. Importing here keeps the module graph linear.
from app.core.csrf import init_csrf_for_templates  # noqa: E402

init_csrf_for_templates(templates)
