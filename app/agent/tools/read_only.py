"""Read-only babyg tool context for creator-owned data.

These helpers are intentionally side-effect free. They collect compact,
prompt-safe summaries from existing services so the first Phase 2 bot loop
can answer using real babyg data before we add Claude tool calling,
streaming, or write approvals.
"""

from __future__ import annotations

from typing import Any

from app.services import bookings, dms, intel, network, performance, profiles, receipts


def collect_context(user_id: str) -> dict[str, Any]:
    """Return all read-only context currently safe for the creator bot."""
    profile = read_my_profile(user_id)
    niches = _as_list(profile.get("niches"))
    tier = str(profile.get("tier") or "basic")
    return {
        "read_my_profile": profile,
        "read_intel_feed": read_intel_feed(niches=niches, tier=tier),
        "read_my_calendar": read_my_calendar(user_id),
        "read_my_dms": read_my_dms(user_id),
        "read_my_receipts": read_my_receipts(user_id),
        "read_my_performance": read_my_performance(user_id),
        "read_creator_directory": read_creator_directory(user_id),
    }


def read_my_profile(user_id: str) -> dict[str, Any]:
    row = profiles.get_creator_profile(user_id) or {}
    location_label = profiles.safe_location_label(row)
    return {
        "name": row.get("full_name"),
        "instagram": row.get("instagram_handle"),
        "location": location_label or "location not set",
        "city": row.get("location_city"),
        "region": row.get("location_region"),
        "country": row.get("location_country"),
        "niches": _as_list(row.get("niches")),
        "formats": _as_list(row.get("content_formats")),
        "topics": _as_list(row.get("topics")),
        "follower_range": row.get("follower_range"),
        "engagement_range": row.get("engagement_range"),
        "years_creating": row.get("creator_tenure"),
        "primary_platform": row.get("primary_platform"),
        "hard_limits": row.get("hard_limits"),
        "writing_samples": _summarize_list(row.get("writing_samples"), limit=3),
        "tier": row.get("tier") or "basic",
    }


def read_intel_feed(
    *, niches: list[str], tier: str, limit: int = 5
) -> list[dict[str, Any]]:
    return [
        {
            "title": row.get("title"),
            "category": row.get("category"),
            "confidence": row.get("confidence"),
            "valid_until": row.get("valid_until"),
            "summary": str(row.get("body") or "")[:320],
        }
        for row in intel.feed_for_creator(niches=niches, tier=tier)[:_bounded_limit(limit)]
    ]


def read_my_calendar(user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    # google_event_id is surfaced so the agent can reference a real
    # Google Calendar event by id when staging calendar.update_event
    # or calendar.delete_event. Local-only items have it as None.
    return [
        {
            "starts_at": row.get("starts_at"),
            "title": row.get("title"),
            "type": row.get("type"),
            "status": row.get("status"),
            "location": row.get("location"),
            "google_event_id": row.get("google_event_id"),
        }
        for row in bookings.list_for_user(
            user_id, horizon="upcoming", limit=_bounded_limit(limit)
        )
    ]


def read_my_dms(user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = dms.list_threads_for_user(user_id)[:_bounded_limit(limit)]
    peer_ids = [str(row.get("peer_id")) for row in rows if row.get("peer_id")]
    peers = profiles.get_creators_by_ids(peer_ids)
    return [
        {
            "peer_id": row.get("peer_id"),
            "peer_name": (peers.get(str(row.get("peer_id"))) or {}).get("full_name"),
            "last_message_at": row.get("last_message_at"),
        }
        for row in rows
    ]


def read_my_receipts(user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "post_type": row.get("post_type"),
            "posted_at": row.get("posted_at"),
            "caption_excerpt": row.get("caption_excerpt"),
            "post_url": row.get("post_url"),
            "like_count": row.get("like_count"),
            "comment_count": row.get("comment_count"),
        }
        for row in receipts.list_for_user(user_id, limit=_bounded_limit(limit))
    ]


def read_my_performance(user_id: str, *, limit: int = 3) -> list[dict[str, Any]]:
    return [
        {
            "week_start_date": row.get("week_start_date"),
            "engagement_rate": row.get("engagement_rate"),
            "follower_delta": row.get("follower_delta"),
            "posts_count": row.get("posts_count"),
            "active_brand_deals_value": row.get("active_brand_deals_value"),
        }
        for row in performance.list_for_user(user_id, limit=_bounded_limit(limit, maximum=12))
    ]


def read_creator_directory(user_id: str, *, limit: int = 6) -> list[dict[str, Any]]:
    return [
        {
            "user_id": row.get("user_id"),
            "name": row.get("full_name"),
            "instagram": row.get("instagram_handle"),
            "location": row.get("location_label"),
            "niches": _as_list(row.get("niches"))[:4],
            "follower_range": row.get("follower_range"),
        }
        for row in network.list_directory_for_creator(user_id)[:_bounded_limit(limit)]
    ]


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _summarize_list(value: Any, *, limit: int) -> list[str]:
    return [item[:500] for item in _as_list(value)[:limit]]


def _bounded_limit(value: Any, *, default: int = 5, maximum: int = 20) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, maximum))
