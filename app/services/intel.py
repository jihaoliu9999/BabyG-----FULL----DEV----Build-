"""Intel post data access — feed for creators, CRUD for operators.

Uses the service-role client so it bypasses RLS. Every caller is gated by
require_role in the route layer; the user_id passed in always comes from
the signed session cookie. Never expose any of these functions over a
public endpoint without an auth check first.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


CATEGORIES = ["venue", "trend", "brand", "collab", "alert"]
CONFIDENCES = ["low", "medium", "high"]
STATUSES = ["draft", "scheduled", "active", "expired", "archived"]
TIERS = ["basic", "pro", "vip"]
FEEDBACK_SIGNALS = ["useful", "not_useful", "acted_on"]


# -----------------------------------------------------------------------------
# Creator feed
# -----------------------------------------------------------------------------


def feed_for_creator(
    *, niches: list[str], tier: str, category: str | None = None
) -> list[dict[str, Any]]:
    """Active, targeted, non-expired intel posts for one creator.

    Filtering happens in two passes: an indexed pg-side filter for status
    and validity, then a Python pass for tier + niche overlap. Volumes are
    small enough that the Python pass is cheap; if this ever shows up in
    a profile we'll move it to a SQL function.
    """
    now_iso = _now_iso()
    try:
        query = (
            supabase_client.get_service_client()
            .table("intel_posts")
            .select("*")
            .eq("status", "active")
            .gt("valid_until", now_iso)
            .order("valid_from", desc=True)
        )
        if category and category in CATEGORIES:
            query = query.eq("category", category)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("intel feed query failed")
        return []

    rows = getattr(result, "data", None) or []
    creator_niches = set(niches or [])
    return [
        row
        for row in rows
        if _matches_creator(row, creator_niches=creator_niches, tier=tier)
    ]


def feedback_for_user(
    user_id: str, intel_post_ids: list[str]
) -> dict[str, str]:
    """Return {intel_post_id: signal} for the given user across these posts."""
    if not intel_post_ids:
        return {}
    try:
        result = (
            supabase_client.get_service_client()
            .table("intel_feedback")
            .select("intel_post_id, signal")
            .eq("user_id", user_id)
            .in_("intel_post_id", intel_post_ids)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("intel feedback lookup failed for %s", user_id)
        return {}
    rows = getattr(result, "data", None) or []
    return {str(r["intel_post_id"]): str(r["signal"]) for r in rows}


def record_feedback(*, user_id: str, intel_post_id: str, signal: str) -> bool:
    if signal not in FEEDBACK_SIGNALS:
        return False
    try:
        supabase_client.get_service_client().table("intel_feedback").upsert(
            {"user_id": user_id, "intel_post_id": intel_post_id, "signal": signal},
            on_conflict="intel_post_id,user_id",
        ).execute()
    except PostgrestAPIError:
        logger.exception(
            "intel feedback upsert failed: user=%s post=%s", user_id, intel_post_id
        )
        return False
    return True


# -----------------------------------------------------------------------------
# Operator CRUD
# -----------------------------------------------------------------------------


def list_for_operator(
    *, status: str | None = None, limit: int = 100
) -> list[dict[str, Any]]:
    try:
        query = (
            supabase_client.get_service_client()
            .table("intel_posts")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if status and status in STATUSES:
            query = query.eq("status", status)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("operator intel list failed")
        return []
    return getattr(result, "data", None) or []


def get_intel_post(post_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("intel_posts")
            .select("*")
            .eq("id", post_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("get_intel_post failed for %s", post_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def create_intel_post(*, created_by: str, payload: dict[str, Any]) -> str | None:
    """Insert one intel post. Returns the new id, or None on failure."""
    body = {**payload, "created_by": created_by}
    try:
        result = (
            supabase_client.get_service_client()
            .table("intel_posts")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("create_intel_post failed")
        return None
    rows = getattr(result, "data", None) or []
    return str(rows[0]["id"]) if rows else None


def update_intel_post(post_id: str, payload: dict[str, Any]) -> bool:
    if not payload:
        return True
    try:
        result = (
            supabase_client.get_service_client()
            .table("intel_posts")
            .update(payload)
            .eq("id", post_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("update_intel_post failed for %s", post_id)
        return False
    return bool(getattr(result, "data", None))


def archive_intel_post(post_id: str) -> bool:
    return update_intel_post(post_id, {"status": "archived"})


def status_counts() -> dict[str, int]:
    """Per-status post count for the operator console tile.

    One indexed COUNT(*) per status (head=True so no rows are pulled).
    Replaces the previous "fetch up to 500 rows then bucket in Python"
    path which grew with the table and ignored anything past 500.
    """
    out: dict[str, int] = {s: 0 for s in STATUSES}
    client = supabase_client.get_service_client()
    for s in STATUSES:
        try:
            result = (
                client.table("intel_posts")
                .select("id", count="exact", head=True)
                .eq("status", s)
                .execute()
            )
        except PostgrestAPIError:
            logger.exception("intel status_counts failed for status=%s", s)
            continue
        out[s] = int(getattr(result, "count", 0) or 0)
    return out


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _matches_creator(
    row: dict[str, Any], *, creator_niches: set[str], tier: str
) -> bool:
    target_tiers = row.get("target_tiers") or []
    if target_tiers and tier not in target_tiers:
        return False
    target_niches = row.get("target_niches") or []
    if not target_niches:
        return True                            # empty == all niches
    return bool(creator_niches.intersection(target_niches))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
