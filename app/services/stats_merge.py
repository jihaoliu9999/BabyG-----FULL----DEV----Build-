"""Combined performance view across manual logs and live Instagram.

The `/creator/performance` page reads manually-logged weeks AND, when
the creator has connected Instagram, recent IG posts + their per-post
insights. This module merges both into one source-tagged list so the
template never has to think about where a row came from.

Hard contract:
  - manual rows always render (the database is authoritative)
  - instagram rows render only when the creator's IG connection is
    real and the Graph call returns successfully. failures fall back
    to manual-only — the page never crashes, never shows fake data,
    and never claims live IG numbers exist when they don't
  - missing per-post insights stay as None in the metrics dict so the
    template renders only what's real
  - rows are sorted by timestamp DESC; manual + IG intermix naturally
    so the creator sees a real time-ordered work history
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.integrations import instagram_meta
from app.services import oauth_connections, performance

logger = logging.getLogger(__name__)


# Default cap on IG posts pulled per page load. Matches the bot tool
# default in services/bot.py:read_my_instagram_stats. Per-page reads
# aren't gated by the 30/day bot cap — that cap protects the agent
# loop, not user-initiated browsing.
DEFAULT_IG_LIMIT = 5
HARD_IG_LIMIT = 10


SOURCE_MANUAL = "manual"
SOURCE_INSTAGRAM = "instagram"


@dataclass(frozen=True)
class StatsRow:
    """One row on the merged performance page."""

    source: str
    title: str
    timestamp: str | None        # ISO 8601, used for sort + render
    permalink: str | None        # IG only
    metrics: dict[str, Any]      # only non-None keys render in the UI
    notes: str | None


def combined_performance(
    user_id: str, *, ig_limit: int = DEFAULT_IG_LIMIT
) -> list[StatsRow]:
    """Return manual + IG rows for the creator, sorted by timestamp DESC.

    `ig_limit` is clamped to HARD_IG_LIMIT to keep a chatty UI from
    hammering the Graph API on every page load.
    """
    rows: list[StatsRow] = []
    rows.extend(_manual_rows(user_id))
    rows.extend(_instagram_rows(user_id, limit=ig_limit))
    rows.sort(key=_sort_key, reverse=True)
    return rows


def has_instagram_data(user_id: str) -> bool:
    """True when the creator has a saved IG connection AND the
    integration is configured. The template uses this to swap the
    page-foot copy. Defensive: any error → False (treat as not
    connected rather than crash the page)."""
    try:
        if not instagram_meta.is_configured():
            return False
        return oauth_connections.get_instagram_connection(user_id) is not None
    except Exception:
        logger.exception("has_instagram_data lookup failed: %s", user_id)
        return False


def _manual_rows(user_id: str) -> list[StatsRow]:
    """Project each performance_logs row into the merged shape. We
    don't catch here — list_for_user already returns [] on failure
    via its own PostgrestAPIError handler."""
    out: list[StatsRow] = []
    for row in performance.list_for_user(user_id):
        week = str(row.get("week_start_date") or "").strip()
        metrics: dict[str, Any] = {
            "engagement_rate": _num_or_none(row.get("engagement_rate")),
            "follower_delta": _num_or_none(row.get("follower_delta")),
            "deal_count": _num_or_none(row.get("active_brand_deals_count")),
            "deal_value": _num_or_none(row.get("active_brand_deals_value")),
        }
        out.append(
            StatsRow(
                source=SOURCE_MANUAL,
                title=f"week of {week}" if week else "weekly log",
                timestamp=week or None,
                permalink=None,
                metrics={k: v for k, v in metrics.items() if v is not None},
                notes=_str_or_none(row.get("notes")),
            )
        )
    return out


def _instagram_rows(user_id: str, *, limit: int) -> list[StatsRow]:
    """Pull recent IG posts + their insights, defensively. Any
    failure path returns [] so the page falls back to manual-only.

    Eligibility check happens BEFORE the Graph call so a creator
    without a connection doesn't burn an unnecessary API hit.
    """
    bounded = max(1, min(int(limit or DEFAULT_IG_LIMIT), HARD_IG_LIMIT))
    try:
        if not instagram_meta.is_configured():
            return []
        connection = oauth_connections.get_instagram_connection(user_id)
        if not connection:
            return []
        ig_user_id = oauth_connections.instagram_account_id(connection)
        if not ig_user_id:
            return []
        token = oauth_connections.access_token_for_instagram(user_id)
        if not token:
            return []
    except Exception:
        # Treat any oauth-storage hiccup as "no IG data" rather than
        # crashing the stats page. Manual rows still render.
        logger.exception("instagram precheck failed: %s", user_id)
        return []

    try:
        media = instagram_meta.get_user_media(
            token, ig_user_id=ig_user_id, limit=bounded
        )
    except instagram_meta.InstagramError:
        # No live data → manual-only. Caller doesn't need to know
        # whether the failure was config, auth, or network.
        return []
    except Exception:
        logger.exception("instagram get_user_media unexpected failure")
        return []

    out: list[StatsRow] = []
    for item in media:
        insights: dict[str, int | None] = {}
        try:
            insights = instagram_meta.get_media_insights(
                token, media_id=item.media_id
            )
        except instagram_meta.InstagramError:
            # Skip insights for this single post but keep the media —
            # like_count + comments_count are still real from the
            # media row itself.
            insights = {}
        except Exception:
            logger.exception(
                "instagram get_media_insights unexpected failure: %s",
                item.media_id,
            )
            insights = {}

        metrics: dict[str, Any] = {
            "likes": item.like_count,
            "comments": item.comments_count,
            "reach": insights.get("reach"),
            "impressions": insights.get("impressions"),
            "engagement": insights.get("engagement"),
            "saved": insights.get("saved"),
        }
        out.append(
            StatsRow(
                source=SOURCE_INSTAGRAM,
                title=_title_for_media(item),
                timestamp=item.timestamp,
                permalink=item.permalink,
                metrics={k: v for k, v in metrics.items() if v is not None},
                notes=None,
            )
        )
    return out


def _title_for_media(item: instagram_meta.InstagramMedia) -> str:
    if item.caption:
        snippet = item.caption.strip().splitlines()[0]
        return snippet[:80].rstrip()
    if item.media_type:
        return item.media_type.lower()
    return "instagram post"


def _sort_key(row: StatsRow) -> tuple[int, str]:
    """Sort by timestamp DESC, putting rows with a real timestamp
    above rows without. The string form is fine — manual weeks are
    YYYY-MM-DD dates, IG timestamps are ISO 8601; both sort
    lexicographically the right way."""
    if not row.timestamp:
        return (0, "")
    return (1, str(row.timestamp))


def _num_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return value
    try:
        s = str(value).strip()
        if not s:
            return None
        if "." in s:
            return float(s)
        return int(s)
    except (TypeError, ValueError):
        return None


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _utc_now_iso() -> str:
    """Exposed so tests can patch the clock. Not currently used in
    production code paths — module just sorts on existing timestamps."""
    return datetime.now(UTC).isoformat()
