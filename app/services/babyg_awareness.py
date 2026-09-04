"""Live-state snapshot babyg reads before every assistant turn.

The bot chat, the composer chip strip, and the proactive nudge stream
all need to know what's happening for a creator *right now*: unread
DMs, freshly accepted connections, upcoming bookings, new discover
matches, brand-trust flips, pending action proposals, matching Hot
Drops, deal-stage changes, unpaid receipts. Rather than have every
caller run its own read pattern, this module produces one compact
dictionary that describes the world.

Every read is wrapped in try/except so a flaky Supabase or a
missing table never blanks the whole snapshot — that source drops out
and the rest still loads. All reads use the service-role client and
respect the same public-projection guards other cross-user surfaces
already apply.

The snapshot is a plain dict of primitive fields (no ORM objects, no
Pydantic models) so it can be injected straight into a Claude system
prompt as JSON and consumed by a deterministic chip generator without
serialization drama.

Never surfaces:
  * exact coordinates
  * baseline metrics (baseline_followers, tier, writing_samples, …)
  * operator-only trust notes
  * raw message content — only counts + peer names via public projection
  * money keywords (relies on the action_proposals gate to keep those out)
"""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# Snapshot cache. Keyed by user_id, value is (built_at_monotonic, dict).
# ~30s TTL keeps repeated bot-chat renders cheap without staleness that
# would materially confuse the user ("babyg says i have 2 unread but
# my inbox shows 3"). Cache is process-local; a multi-process deploy
# will do a duplicate read per box, which is fine.
_TTL_SECONDS = 30.0
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def snapshot(user_id: str, *, force: bool = False) -> dict[str, Any]:
    """Return the compact awareness snapshot for ``user_id``.

    Pass ``force=True`` to bypass the 30s cache — used by the chip
    generator immediately after a user action so the next render
    reflects reality.
    """
    if not user_id:
        return _empty()
    now = time.monotonic()
    if not force:
        hit = _CACHE.get(user_id)
        if hit is not None and (now - hit[0]) < _TTL_SECONDS:
            return hit[1]
    built = _build(user_id)
    _CACHE[user_id] = (now, built)
    return built


def invalidate(user_id: str) -> None:
    """Drop the cached snapshot for ``user_id``.

    Called by routes that mutate state (send DM, confirm action, save
    booking) so the next bot-chat render doesn't quote a value that's
    already stale.
    """
    _CACHE.pop(user_id, None)


def _empty() -> dict[str, Any]:
    return {
        "unread_dms": {"count": 0, "latest_peer_name": None},
        "recent_connection_accepted": None,
        "recent_incoming_connection": None,
        "next_booking": None,
        "pending_booking": None,
        "fresh_discover_match": None,
        "pending_action_proposal": None,
        "recent_hot_drop": None,
        "open_deal_stage": None,
    }


def _build(user_id: str) -> dict[str, Any]:
    """Assemble the snapshot. Every helper is safe-to-fail."""
    return {
        "unread_dms": _unread_dms(user_id),
        "recent_connection_accepted": _recent_connection_accepted(user_id),
        "recent_incoming_connection": _recent_incoming_connection(user_id),
        "next_booking": _next_booking(user_id),
        "pending_booking": _pending_booking(user_id),
        "fresh_discover_match": _fresh_discover_match(user_id),
        "pending_action_proposal": _pending_action_proposal(user_id),
        "recent_hot_drop": _recent_hot_drop(user_id),
        "open_deal_stage": _open_deal_stage(user_id),
    }


# ---------------------------------------------------------------------------
# Signal readers — each returns a small dict or None, never raises.
# ---------------------------------------------------------------------------


def _unread_dms(user_id: str) -> dict[str, Any]:
    """Unread DM count + first name of the most-recent peer for chip copy."""
    try:
        from app.services import dms, profiles
    except Exception:
        return {"count": 0, "latest_peer_name": None}
    try:
        count = dms.unread_count_for_user(user_id)
    except Exception:
        count = 0
    peer_name: str | None = None
    try:
        threads = dms.list_threads_for_user(user_id) or []
        peer_ids = [
            str(t.get("peer_id")) for t in threads if t.get("peer_id")
        ]
        # One .in_() over all peers instead of N per-thread reads. We
        # only need `full_name` from the public projection to pick copy
        # for the awareness chip.
        peers_by_id = (
            profiles.get_creators_by_ids(peer_ids) if peer_ids else {}
        )
        for pid in peer_ids:
            peer = peers_by_id.get(pid)
            if not peer:
                continue
            name = (peer.get("full_name") or "").strip()
            if name:
                peer_name = name
                break
    except Exception:
        peer_name = None
    return {"count": int(count or 0), "latest_peer_name": peer_name}


def _recent_connection_accepted(user_id: str) -> dict[str, Any] | None:
    """Most recent connection where the peer accepted the request I sent."""
    try:
        from app.services import network, profiles
    except Exception:
        return None
    try:
        accepted = network.list_accepted_for_user(user_id) or []
    except Exception:
        return None
    horizon = datetime.now(UTC) - timedelta(hours=72)
    # Batch-fetch every candidate peer up-front so a run of stale rows
    # doesn't trigger N per-row supabase reads on the way to the first
    # eligible one.
    candidates: list[tuple[dict, str]] = []
    for row in accepted[:5]:
        accepted_at = _parse_iso(row.get("accepted_at") or row.get("updated_at"))
        if accepted_at is None or accepted_at < horizon:
            continue
        peer_id = row.get("addressee_id") if row.get("requester_id") == user_id else (
            row.get("requester_id") if row.get("addressee_id") == user_id else None
        )
        if not peer_id:
            continue
        candidates.append((row, str(peer_id)))
    if not candidates:
        return None
    try:
        peers_by_id = profiles.get_creators_by_ids(
            [pid for _, pid in candidates]
        )
    except Exception:
        peers_by_id = {}
    for _row, peer_id in candidates:
        peer = peers_by_id.get(peer_id)
        if not peer:
            continue
        return {
            "peer_id": peer_id,
            "peer_name": (peer.get("full_name") or "").strip() or None,
            "peer_handle": (peer.get("instagram_handle") or "").strip() or None,
        }
    return None


def _recent_incoming_connection(user_id: str) -> dict[str, Any] | None:
    """Most recent pending connection request addressed to me."""
    try:
        from app.services import network, profiles
    except Exception:
        return None
    try:
        incoming = network.list_incoming_pending(user_id) or []
    except Exception:
        return None
    peer_ids = [
        str(row.get("requester_id")) for row in incoming[:3] if row.get("requester_id")
    ]
    if not peer_ids:
        return None
    try:
        peers_by_id = profiles.get_creators_by_ids(peer_ids)
    except Exception:
        peers_by_id = {}
    for peer_id in peer_ids:
        peer = peers_by_id.get(peer_id)
        if not peer:
            continue
        return {
            "peer_id": peer_id,
            "peer_name": (peer.get("full_name") or "").strip() or None,
        }
    return None


def _next_booking(user_id: str) -> dict[str, Any] | None:
    """Next confirmed booking in the next 24 hours."""
    try:
        from app.services import bookings
    except Exception:
        return None
    try:
        upcoming = bookings.list_for_user(user_id, horizon="upcoming", limit=6) or []
    except Exception:
        return None
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=24)
    for b in upcoming:
        starts_at = _parse_iso(b.get("starts_at"))
        if starts_at is None or starts_at > horizon:
            continue
        if (b.get("status") or "").lower() == "cancelled":
            continue
        minutes_out = max(0, int((starts_at - now).total_seconds() // 60))
        return {
            "id": b.get("id"),
            "title": (b.get("title") or "").strip() or "an event",
            "starts_at": b.get("starts_at"),
            "minutes_until_start": minutes_out,
            "venue_name": (b.get("venue_name") or "").strip() or None,
        }
    return None


def _pending_booking(user_id: str) -> dict[str, Any] | None:
    """First still-pending booking in the next 48h — booking_pending nudge signal."""
    try:
        from app.services import bookings
    except Exception:
        return None
    try:
        upcoming = bookings.list_for_user(user_id, horizon="upcoming", limit=12) or []
    except Exception:
        return None
    now = datetime.now(UTC)
    horizon = now + timedelta(hours=48)
    for b in upcoming:
        if (b.get("status") or "").lower() != "pending":
            continue
        starts_at = _parse_iso(b.get("starts_at"))
        if starts_at is None or starts_at > horizon:
            continue
        return {
            "id": b.get("id"),
            "title": (b.get("title") or "").strip() or "a booking",
            "starts_at": b.get("starts_at"),
        }
    return None


def _fresh_discover_match(user_id: str) -> dict[str, Any] | None:
    """Most recent discover card younger than 72h — reuses bot_nudges freshness."""
    try:
        from app.services import discover
    except Exception:
        return None
    try:
        cards = discover.list_cards(
            viewer_id=user_id, viewer_role="creator", kind="all", limit=6
        ) or []
    except Exception:
        return None
    horizon = datetime.now(UTC) - timedelta(hours=72)
    for card in cards:
        created_at = _parse_iso(card.get("created_at"))
        if created_at is None or created_at < horizon:
            continue
        card_id = card.get("card_id") or card.get("id")
        if not card_id:
            continue
        return {
            "card_id": card_id,
            "card_kind": card.get("card_kind") or "match",
            "title": (card.get("title") or "").strip() or "a new match",
            "subtitle": (card.get("subtitle") or "").strip() or None,
        }
    return None


def _pending_action_proposal(user_id: str) -> dict[str, Any] | None:
    """Oldest still-pending action_proposal awaiting confirm."""
    try:
        from app.core import supabase_client
    except Exception:
        return None
    try:
        client = supabase_client.get_service_client()
        result = (
            client.table("action_proposals")
            .select("id, action_type, action_category, created_at, expires_at")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("created_at", desc=False)
            .limit(1)
            .execute()
        )
    except Exception:
        return None
    rows = getattr(result, "data", None) or []
    if not rows:
        return None
    row = rows[0]
    return {
        "id": row.get("id"),
        "action_type": row.get("action_type"),
        "action_category": row.get("action_category"),
    }


def _recent_hot_drop(user_id: str) -> dict[str, Any] | None:
    """Most recent active intel_post that matches at least one of my niches."""
    try:
        from app.services import intel, profiles
    except Exception:
        return None
    try:
        profile = profiles.get_creator_profile(user_id) or {}
    except Exception:
        profile = {}
    niches = list(profile.get("niches") or [])
    tier = profile.get("tier") or "basic"
    try:
        posts = intel.feed_for_creator(niches=niches, tier=tier) or []
    except Exception:
        return None
    for post in posts[:3]:
        created_at = _parse_iso(post.get("valid_from") or post.get("created_at"))
        if created_at is None:
            continue
        # Only surface if fresh enough to feel worth mentioning.
        if datetime.now(UTC) - created_at > timedelta(hours=48):
            continue
        return {
            "id": post.get("id"),
            "title": (post.get("title") or "").strip() or "a new hot drop",
            "category": post.get("category"),
        }
    return None


def _open_deal_stage(user_id: str) -> str | None:
    """Highest-priority open deal stage across the user's DM briefs."""
    try:
        from app.core import supabase_client
    except Exception:
        return None
    try:
        client = supabase_client.get_service_client()
        # dm_ai_briefs is recipient-private — the RLS on it already
        # restricts to the recipient, but the service client bypasses
        # RLS so we filter by recipient explicitly.
        result = (
            client.table("dm_ai_briefs")
            .select("deal_stage")
            .eq("recipient_id", user_id)
            .not_.is_("deal_stage", "null")
            .limit(20)
            .execute()
        )
    except Exception:
        return None
    rows = getattr(result, "data", None) or []
    priority = [
        "waiting_terms",
        "negotiating",
        "qualifying",
        "new_inquiry",
        "scheduled",
        "risky_hold",
    ]
    seen = {r.get("deal_stage") for r in rows if isinstance(r, dict)}
    for stage in priority:
        if stage in seen:
            return stage
    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None


def snapshot_summary_lines(snap: dict[str, Any]) -> list[str]:
    """Render the snapshot as short bullet lines for the system prompt.

    Kept human-readable so a future prompt tweak can eyeball the
    injected context without parsing JSON. Empty signals drop out so
    babyg doesn't see a wall of Nones and hallucinate detail from
    absence.
    """
    lines: list[str] = []
    dms_sig = snap.get("unread_dms") or {}
    if dms_sig.get("count"):
        peer = dms_sig.get("latest_peer_name")
        peer_tag = f" (most recent: {peer})" if peer else ""
        lines.append(f"- {dms_sig['count']} unread DM(s){peer_tag}")
    accepted = snap.get("recent_connection_accepted")
    if accepted and accepted.get("peer_name"):
        lines.append(f"- {accepted['peer_name']} just accepted your connection request")
    incoming = snap.get("recent_incoming_connection")
    if incoming and incoming.get("peer_name"):
        lines.append(f"- pending connection request from {incoming['peer_name']}")
    next_book = snap.get("next_booking")
    if next_book:
        title = next_book.get("title")
        minutes = next_book.get("minutes_until_start")
        if minutes is not None and minutes < 240:
            lines.append(f"- {title} starts in ~{minutes} min")
    pending_book = snap.get("pending_booking")
    if pending_book:
        lines.append(f"- pending booking not yet confirmed: {pending_book.get('title')}")
    fresh = snap.get("fresh_discover_match")
    if fresh:
        lines.append(f"- fresh discover match: {fresh.get('title')}")
    ap = snap.get("pending_action_proposal")
    if ap:
        lines.append(f"- action awaiting your confirm: {ap.get('action_type')}")
    hot = snap.get("recent_hot_drop")
    if hot:
        lines.append(f"- new hot drop matching your niches: {hot.get('title')}")
    stage = snap.get("open_deal_stage")
    if stage:
        lines.append(f"- an open deal is at stage: {stage}")
    return lines
