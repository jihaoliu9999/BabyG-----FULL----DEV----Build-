"""Profile views — write on view, tier-gated read on the viewee side.

Per PRD §10.2 the visibility rules are:
  basic  — sees nothing about who viewed them
  pro    — sees the count of distinct viewers in the last 30 days
  vip    — sees the full list (viewer + viewed_at)

We always write the row regardless of the viewee's tier so an upgrade
later surfaces historical data. Self-views are dropped at the route
layer; we don't double-check here because the schema CHECK already
forbids viewer_id = viewed_id.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


VISIBILITY_WINDOW_DAYS = 30


def record_view(*, viewer_id: str, viewed_id: str) -> bool:
    if viewer_id == viewed_id or not viewer_id or not viewed_id:
        return False
    # Per-day dedupe: the unique index on (viewer_id, viewed_id, date(viewed_at))
    # rejects repeat hits during the same UTC day. Catch the conflict and
    # treat it as a successful no-op so the route layer doesn't surface an
    # error for the legitimate "same person reloaded twice" path.
    try:
        supabase_client.get_service_client().table("profile_views").insert(
            {"viewer_id": viewer_id, "viewed_id": viewed_id}
        ).execute()
    except PostgrestAPIError as e:
        if _is_unique_violation(e):
            return True
        # Best-effort. If we can't write a view we still want to render
        # the profile.
        logger.exception("record_view failed: %s -> %s", viewer_id, viewed_id)
        return False
    return True


def _is_unique_violation(err: PostgrestAPIError) -> bool:
    # Postgres SQLSTATE for unique_violation is 23505. supabase-py surfaces
    # the original code on the APIError under .code (string).
    return getattr(err, "code", "") == "23505"


def count_distinct_viewers(viewed_id: str, *, days: int = VISIBILITY_WINDOW_DAYS) -> int:
    """Pro tier: count of distinct viewers in the recent window."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("profile_views")
            .select("viewer_id")
            .eq("viewed_id", viewed_id)
            .gte("viewed_at", cutoff)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("count_distinct_viewers failed: %s", viewed_id)
        return 0
    rows = getattr(result, "data", None) or []
    return len({r["viewer_id"] for r in rows})


def list_recent_viewers(
    viewed_id: str, *, days: int = VISIBILITY_WINDOW_DAYS, limit: int = 100
) -> list[dict[str, Any]]:
    """VIP tier: most-recent view per distinct viewer in the window."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("profile_views")
            .select("*")
            .eq("viewed_id", viewed_id)
            .gte("viewed_at", cutoff)
            .order("viewed_at", desc=True)
            .limit(limit * 4)         # over-fetch to dedupe in Python
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("list_recent_viewers failed: %s", viewed_id)
        return []
    rows = getattr(result, "data", None) or []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for r in rows:
        vid = str(r["viewer_id"])
        if vid in seen:
            continue
        seen.add(vid)
        out.append(r)
        if len(out) >= limit:
            break
    return out
