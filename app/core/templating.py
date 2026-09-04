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


_MONTH_ABBR = (
    "jan", "feb", "mar", "apr", "may", "jun",
    "jul", "aug", "sep", "oct", "nov", "dec",
)


def _short_dt(value):
    """Render a Postgres ISO timestamptz as `Mon D, YYYY · h:MMam`.

    Human date + 12-hour time. Nobody reads `2026-09-02 14:00` fast
    when it says `sep 2, 2026 · 2:00pm`. Falls back to the raw string
    if parsing fails so an unexpected shape still renders something.
    """
    if not value:
        return ""
    raw = str(value)
    # Grab the first 16 chars — enough for YYYY-MM-DDTHH:MM regardless
    # of trailing tz suffix. If the shape isn't ISO, we return raw.
    try:
        date_part, _, time_part = raw[:16].replace("T", " ").partition(" ")
        y, m, d = date_part.split("-")
        month = _MONTH_ABBR[int(m) - 1]
        date_out = f"{month} {int(d)}, {y}"
        if not time_part:
            return date_out
        hh, mm = time_part.split(":")
        hour = int(hh)
        suffix = "am" if hour < 12 else "pm"
        hour = hour % 12 or 12
        return f"{date_out} · {hour}:{mm}{suffix}"
    except (ValueError, IndexError):
        return raw


def _short_date(value):
    """Render a date (or leading date portion of an ISO timestamp) as
    `Mon D, YYYY`. e.g. `sep 2, 2026`."""
    if not value:
        return ""
    raw = str(value)
    try:
        y, m, d = raw[:10].split("-")
        month = _MONTH_ABBR[int(m) - 1]
        return f"{month} {int(d)}, {y}"
    except (ValueError, IndexError):
        return raw


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


def _human_ago(value):
    """Render an ISO timestamp as a compact relative label.

    Examples:
      < 1 min  -> "now"
      < 1 hour -> "12m"
      < 24 h   -> "3h"
      < 7 days -> "2d"
      same year -> "may 24"
      else     -> "may 24, 2025"

    Falls back to the raw string on parse failure so a bad row never
    500s the page. Absent value renders as empty string.
    """
    if not value:
        return ""
    from datetime import UTC, datetime

    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return text[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    delta = now - parsed
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return "now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    if days < 7:
        return f"{days}d"
    month = parsed.strftime("%b").lower()
    if parsed.year == now.year:
        return f"{month} {parsed.day}"
    return f"{month} {parsed.day}, {parsed.year}"


def _current_role(request) -> str | None:
    """Return the signed-in role (``creator``/``brand``/``operator``) or
    ``None`` for anonymous. Used by base.html to render the role pill in
    the sidebar/topbar without every route having to thread ``session``
    into its template context.

    Lazy import breaks the templating <-> security circular that would
    happen at module-load time.
    """
    from app.core.security import read_session

    try:
        session = read_session(request)
    except Exception:
        return None
    return session["role"] if session else None


def _current_profile(request):
    """Return the signed-in creator profile (dict) or ``None``.

    Base.html uses this to render the top-right profile avatar on
    EVERY creator page without every route having to thread ``profile``
    into its context. Previously, routes that forgot the ``profile``
    key made the avatar fall back to "creator" → "C" while nearby
    pages showed the real photo — the inconsistency the user reported.

    Cached on ``request.state.current_profile`` for the duration of
    the request so repeated calls in a single template render don't
    stack DB lookups. Only fires for creator sessions; brand/operator
    return None.
    """
    from app.core.security import read_session
    from app.services import profiles

    if hasattr(request, "state") and hasattr(request.state, "current_profile"):
        return request.state.current_profile

    resolved = None
    try:
        session = read_session(request)
        if session and session.get("role") == "creator":
            resolved = (
                profiles.get_creator_profile_cached(
                    session["user_id"], request
                )
                or None
            )
    except Exception:
        resolved = None

    if hasattr(request, "state"):
        import contextlib
        with contextlib.suppress(Exception):
            request.state.current_profile = resolved
    return resolved


def _dm_time(value):
    """Render an ISO timestamp as a compact tail time — '3:31pm'.

    Used under grouped DM bubbles where we already show a day header
    above the group, so the reader only needs the hour:minute. Falls
    back to empty on a bad value so a stray row can never 500 the render.
    """
    if not value:
        return ""
    from datetime import UTC, datetime

    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.strftime("%-I:%M%p").lower()


def _dm_day_sep(value):
    """Day separator label above a group of DM bubbles.

    Examples:
      today            -> "today"
      yesterday        -> "yesterday"
      within 7 days    -> "monday"
      same year        -> "jun 5"
      else             -> "jun 5, 2024"

    Fallback: empty. Not for anything smaller than a day.
    """
    if not value:
        return ""
    from datetime import UTC, datetime, timedelta

    text = str(value).strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    parsed_day = parsed.date()
    today = now.date()
    if parsed_day == today:
        return "today"
    if parsed_day == today - timedelta(days=1):
        return "yesterday"
    if (today - parsed_day).days < 7:
        return parsed.strftime("%A").lower()
    if parsed.year == now.year:
        return parsed.strftime("%b %-d").lower()
    return parsed.strftime("%b %-d, %Y").lower()


def _cached_state_int(request, attr: str):
    """Read an int from request.state, tolerating MagicMock and missing state.

    We can't just `hasattr(request.state, attr)` because a MagicMock
    (used in a few tests that render partials directly) returns True
    for every attribute lookup. Explicitly check for an int in the
    attribute's __dict__ so only real writes count as cache hits.
    """
    state = getattr(request, "state", None)
    if state is None:
        return None
    stash = getattr(state, "__dict__", None)
    if not isinstance(stash, dict):
        return None
    value = stash.get(attr)
    return value if isinstance(value, int) else None


def _store_state_int(request, attr: str, value: int) -> None:
    state = getattr(request, "state", None)
    if state is None:
        return
    import contextlib

    with contextlib.suppress(Exception):
        setattr(state, attr, int(value))


def _pending_action_count(request) -> int:
    """Number of pending babyg action proposals waiting on this creator.

    Used by creator_tabbar.html to render a badge on the babyg tab so a
    creator sees "N drafts babyg wants you to review" without opening
    chat. Cached on request.state so the tabbar doesn't hit supabase
    a second time when a page also queries pending actions itself
    (e.g. the home dashboard's "needs you" rail).

    Returns 0 for anon, brand, and operator sessions — the badge only
    makes sense for a signed-in creator.
    """
    from app.core.security import read_session

    cached = _cached_state_int(request, "pending_action_count")
    if cached is not None:
        return cached

    resolved = 0
    try:
        session = read_session(request)
        if session and session.get("role") == "creator":
            from app.services import action_proposals

            resolved = int(
                action_proposals.count_pending_for_user(
                    user_id=session["user_id"]
                )
                or 0
            )
    except Exception:
        resolved = 0

    _store_state_int(request, "pending_action_count", resolved)
    return resolved


def _unread_dm_count(request) -> int:
    """Unread DM count for the current creator, cached per request.

    Mirrors the brand-side value that brand.py already passes to its
    tabbar. Making it a template global means the creator tabbar picks
    it up on every page without every creator route having to thread
    unread_dms into its template context.
    """
    from app.core.security import read_session

    cached = _cached_state_int(request, "unread_dm_count")
    if cached is not None:
        return cached

    resolved = 0
    try:
        session = read_session(request)
        if session and session.get("role") == "creator":
            from app.services import dms

            resolved = int(dms.unread_count_for_user(session["user_id"]) or 0)
    except Exception:
        resolved = 0

    _store_state_int(request, "unread_dm_count", resolved)
    return resolved


templates.env.filters["short_dt"] = _short_dt
templates.env.filters["short_date"] = _short_date
templates.env.filters["safe_url"] = _safe_url
templates.env.filters["bot_markdown"] = _bot_markdown
templates.env.filters["human_ago"] = _human_ago
templates.env.filters["dm_time"] = _dm_time
templates.env.filters["dm_day_sep"] = _dm_day_sep
templates.env.globals["asset_url"] = asset_url
templates.env.globals["current_role"] = _current_role
templates.env.globals["current_profile"] = _current_profile
templates.env.globals["pending_action_count"] = _pending_action_count
templates.env.globals["unread_dm_count"] = _unread_dm_count

# Lazy-import to avoid a circular: csrf.py imports from app.config which is
# safe, but app.core.security imports app.config too and we don't want any
# template-time gotchas. Importing here keeps the module graph linear.
from app.core.csrf import init_csrf_for_templates  # noqa: E402

init_csrf_for_templates(templates)
