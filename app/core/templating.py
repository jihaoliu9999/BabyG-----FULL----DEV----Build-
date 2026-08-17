"""Single Jinja2Templates instance shared across all routers."""

from __future__ import annotations

import hashlib
import re
from html import escape as _html_escape
from pathlib import Path

from fastapi.templating import Jinja2Templates
from markupsafe import Markup

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))


# Compute content hashes once at import time so HTML always emits a
# fresh `?v=…` query whenever a static file changes. Pairs with the
# 1-hour `immutable` Cache-Control on /static — browsers can keep
# old hashes cached forever, and pick up new hashes on next render.
def _asset_hashes() -> dict[str, str]:
    hashes: dict[str, str] = {}
    if not STATIC_DIR.exists():
        return hashes
    for path in STATIC_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(STATIC_DIR).as_posix()
        digest = hashlib.sha1(path.read_bytes()).hexdigest()[:8]
        hashes[rel] = digest
    return hashes


_ASSET_HASHES = _asset_hashes()


def asset_url(path: str) -> str:
    """Return `/static/<path>?v=<hash>` for cache-busted asset URLs.

    Missing files fall through to `/static/<path>` (no version), so
    typos surface as a normal 404 rather than a silent server error.
    """
    clean = path.lstrip("/")
    digest = _ASSET_HASHES.get(clean)
    if digest is None:
        return f"/static/{clean}"
    return f"/static/{clean}?v={digest}"


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


_BULLET_RE = re.compile(r"^\s*[-*]\s+(.*)$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _bot_markdown(value):
    """Render a safe, opinionated subset of markdown for assistant replies.

    Handles:
      - blank-line-separated paragraphs → ``<p>``
      - lines beginning with ``- `` or ``* `` in a run → ``<ul><li>``
      - single line breaks inside a paragraph → ``<br>``
      - ``**bold**`` → ``<strong>``

    Everything else is HTML-escaped before any tag is added, so an LLM
    can never emit executable markup even if it tries. Deliberately does
    NOT support links, images, inline code, or blockquotes — the bot's
    output stays within the shapes we've tested end-to-end.
    """
    if not value:
        return Markup("")
    text = str(value).replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return Markup("")

    def _inline(s: str) -> str:
        s = _html_escape(s, quote=False)
        return _BOLD_RE.sub(r"<strong>\1</strong>", s)

    blocks_html: list[str] = []
    for block in re.split(r"\n\s*\n", text):
        lines = [line for line in block.split("\n") if line.strip() != ""]
        if not lines:
            continue
        matches = [_BULLET_RE.match(line) for line in lines]
        if matches and all(m is not None for m in matches):
            items = "".join(f"<li>{_inline(m.group(1))}</li>" for m in matches if m)
            blocks_html.append(f"<ul>{items}</ul>")
        else:
            body = "<br>".join(_inline(line) for line in lines)
            blocks_html.append(f"<p>{body}</p>")
    return Markup("".join(blocks_html))


templates.env.filters["short_dt"] = _short_dt
templates.env.filters["short_date"] = _short_date
templates.env.filters["safe_url"] = _safe_url
templates.env.filters["bot_markdown"] = _bot_markdown
templates.env.globals["asset_url"] = asset_url

# Lazy-import to avoid a circular: csrf.py imports from app.config which is
# safe, but app.core.security imports app.config too and we don't want any
# template-time gotchas. Importing here keeps the module graph linear.
from app.core.csrf import init_csrf_for_templates  # noqa: E402

init_csrf_for_templates(templates)
