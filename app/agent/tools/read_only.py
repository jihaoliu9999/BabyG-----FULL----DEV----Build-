"""Read-only babyg tool context for creator-owned data.

These helpers are intentionally side-effect free. They collect compact,
prompt-safe summaries from existing services so the first Phase 2 bot loop
can answer using real babyg data before we add Claude tool calling,
streaming, or write approvals.
"""

from __future__ import annotations

from typing import Any

from app.services import (
    babyg_deals,
    babyg_memory,
    babyg_relations,
    bookings,
    dms,
    intel,
    network,
    performance,
    profiles,
    receipts,
)


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


def read_relationship_notes(
    user_id: str,
    *,
    brand: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return relationship notes for the creator, newest first."""
    rows = babyg_relations.list_relationship_notes(
        user_id,
        brand_name=brand,
        kind=kind,
        limit=_bounded_limit(limit, default=10, maximum=25),
    )
    return [
        {
            "id": row.get("id"),
            "kind": row.get("kind"),
            "body": row.get("body"),
            "brand_name": row.get("brand_name"),
            "peer_id": row.get("peer_id"),
            "babyg_source": row.get("babyg_source"),
            "created_at": row.get("created_at"),
        }
        for row in rows
    ]


def read_my_deals(
    user_id: str,
    *,
    brand: str | None = None,
    stage: str | None = None,
    active_only: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the creator's deal pipeline. Case-insensitive brand
    filter; stage filter; active_only hides terminal stages."""
    rows = babyg_deals.list_deals(
        user_id,
        stage=stage,
        active_only=active_only,
        limit=_bounded_limit(limit, default=20, maximum=50),
    )
    if brand:
        needle = " ".join(brand.strip().lower().split())
        if needle:
            rows = [
                r
                for r in rows
                if needle in str(r.get("brand_name") or "").lower()
            ]
    return [
        {
            "id": row.get("id"),
            "brand_name": row.get("brand_name"),
            "stage": row.get("stage"),
            "agreed_amount_cents": row.get("agreed_amount_cents"),
            "paid_amount_cents": row.get("paid_amount_cents"),
            "deliverables": row.get("deliverables"),
            "usage_rights": row.get("usage_rights"),
            "platform": row.get("platform"),
            "deadline": row.get("deadline"),
            "payment_terms": row.get("payment_terms"),
            "handles": row.get("handles") or [],
            "emails": row.get("emails") or [],
            "first_touch_at": row.get("first_touch_at"),
            "last_touch_at": row.get("last_touch_at"),
        }
        for row in rows
    ]


def read_my_drafts(
    user_id: str,
    *,
    match: str | None = None,
    status: str | None = None,
    channel: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return recent babyg-composed drafts for the creator. Optional
    substring/status/channel filters. Prompt-safe shape — body is
    truncated so the tool result stays small."""
    rows = babyg_memory.list_drafts(
        user_id,
        match=match,
        status=status,
        channel=channel,
        limit=_bounded_limit(limit, default=10, maximum=25),
    )
    result: list[dict[str, Any]] = []
    for row in rows:
        body = str(row.get("body") or "")
        result.append(
            {
                "id": row.get("id"),
                "status": row.get("status"),
                "channel": row.get("channel"),
                "origin_tool": row.get("origin_tool"),
                "to": row.get("to_addr"),
                "subject": row.get("subject"),
                "body": body[:1200],
                "body_truncated": len(body) > 1200,
                "gmail_message_id": row.get("gmail_message_id"),
                "updated_at": row.get("updated_at"),
                "sent_at": row.get("sent_at"),
            }
        )
    return result


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
