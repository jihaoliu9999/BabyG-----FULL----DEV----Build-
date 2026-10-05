"""Unified creator, brand, and opportunity discovery.

This service is additive to the legacy creator-only discovery module. It reads
the public-safe ``discovery_cards`` view and records mixed-kind actions in the
extended ``creator_discovery_actions`` ledger. No private profile fields cross
this boundary.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import brand_trust, discover_insights

logger = logging.getLogger(__name__)

CardKind = Literal["creator", "brand", "opportunity"]
CardAction = Literal[
    "viewed",
    "passed",
    "saved",
    "connected",
    "interested",
    "opened_profile",
    "undo_pass",
]

CARD_KINDS: Final[frozenset[str]] = frozenset({"creator", "brand", "opportunity"})
FILTER_KINDS: Final[frozenset[str]] = frozenset({"all", *CARD_KINDS})
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {
        "viewed",
        "passed",
        "saved",
        "connected",
        "interested",
        "opened_profile",
        "undo_pass",
    }
)
PASSED_COOLDOWN_DAYS: Final = 30
DEFAULT_LIMIT: Final = 12
HARD_LIMIT: Final = 30
# Hard cap for the action-history read used to compute exclusions. The
# ~30-day `passed` cooldown plus permanent commits (saved/connected/
# interested) fit comfortably inside this window for any realistic user,
# and it bounds the read so a power user with thousands of swipes
# doesn't force a full-table scan on every discover render.
_EXCLUSION_HISTORY_CAP: Final = 500


def list_cards(
    *,
    viewer_id: str,
    viewer_role: str,
    kind: str = "all",
    category: str | None = None,
    location: str | None = None,
    budget_min: int | None = None,
    budget_max: int | None = None,
    viewer_tags: list[str] | None = None,
    viewer_location_label: str | None = None,
    viewer_platform: str | None = None,
    viewer_deal_type_preferences: list[str] | None = None,
    limit: int = DEFAULT_LIMIT,
    prioritize: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return a filtered, public-safe mixed card stack.

    The view owns the cross-table projection. This function only applies
    viewer-specific filters and action-history exclusions.

    ``viewer_deal_type_preferences`` is a soft ordering signal only.
    When non-empty, opportunity cards whose ``listing_type`` matches one
    of the preferences are pulled to the front while preserving the
    existing relative order within each group. Every other card
    (opportunities that don't match, plus every creator/brand card) is
    still returned in its original position — this is preference-first
    ordering, never a hard filter. Empty/None or a card with a null
    ``listing_type`` falls back to the current ordering exactly.
    """
    uid = safe_uuid(viewer_id)
    if not uid or viewer_role not in {"creator", "brand"}:
        return []
    kind_clean = clean_kind(kind)
    bounded = max(1, min(int(limit or DEFAULT_LIMIT), HARD_LIMIT))
    # Keep an exclusion buffer above the caller's requested count so
    # cards the viewer has already passed/saved/connected/interested on
    # don't starve small slices (e.g. the home preview asks for 3).
    # Capped at HARD_LIMIT*4 so heavy callers don't grow unbounded.
    fetch_limit = min(max(bounded * 3, bounded + 10), HARD_LIMIT * 4)

    try:
        query = (
            supabase_client.get_service_client()
            .table("discovery_cards")
            .select("*")
            .neq("owner_user_id", uid)
            .order("created_at", desc=True)
            .limit(fetch_limit)
        )
        if kind_clean != "all":
            query = query.eq("card_kind", kind_clean)
        if category:
            query = query.contains("tags", [category.strip().lower()[:40]])
        if location:
            query = query.ilike("location_label", f"%{location.strip()[:80]}%")
        if budget_min is not None:
            query = query.gte("budget_max", max(0, budget_min))
        if budget_max is not None:
            query = query.lte("budget_min", max(0, budget_max))
        result = query.execute()
    except Exception:
        logger.exception("unified discovery card lookup failed for %s", viewer_id)
        return []

    excluded = _excluded_card_keys(uid)
    normalized: list[dict[str, Any]] = []
    for raw in getattr(result, "data", None) or []:
        card = _normalize_card(
            raw,
            viewer_tags=viewer_tags or [],
            viewer_location_label=viewer_location_label,
            viewer_platform=viewer_platform,
        )
        if card is None:
            continue
        key = (card["card_kind"], card["card_id"])
        if key in excluded:
            continue
        normalized.append(card)

    if prioritize:
        normalized.sort(
            key=lambda card: (
                card["card_kind"] != prioritize[0]
                or card["card_id"] != prioritize[1]
            )
        )

    # Preference-first ordering. Additive to the caller-provided
    # ordering — a stable partition into (matching opportunity, other),
    # keeping the existing sequence within each group. Guarded so a
    # creator with no saved preferences, an empty list, or values the
    # view can't tell us about (missing listing_type column pre-0046)
    # sees zero behavior change.
    preferred = {
        str(v).strip().lower()
        for v in (viewer_deal_type_preferences or [])
        if isinstance(v, str) and str(v).strip()
    }
    if preferred:
        matching: list[dict[str, Any]] = []
        rest: list[dict[str, Any]] = []
        for card in normalized:
            listing_type = str(card.get("listing_type") or "").strip().lower()
            if (
                card.get("card_kind") == "opportunity"
                and listing_type
                and listing_type in preferred
            ):
                matching.append(card)
            else:
                rest.append(card)
        normalized = matching + rest

    return normalized[:bounded]


def get_card(
    *,
    card_kind: str,
    card_id: str,
    viewer_tags: list[str] | None = None,
    viewer_location_label: str | None = None,
    viewer_platform: str | None = None,
) -> dict[str, Any] | None:
    kind = clean_kind(card_kind)
    cid = safe_uuid(card_id)
    if kind == "all" or not cid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("discovery_cards")
            .select("*")
            .eq("card_kind", kind)
            .eq("card_id", cid)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("unified discovery card lookup failed: %s %s", kind, cid)
        return None
    rows = getattr(result, "data", None) or []
    if not rows:
        return None
    return _normalize_card(
        rows[0],
        viewer_tags=viewer_tags or [],
        viewer_location_label=viewer_location_label,
        viewer_platform=viewer_platform,
    )


def get_opportunity_cards(listing_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Live opportunity cards for the given listing ids, keyed by id.

    Reads the same public-safe ``discovery_cards`` view and runs the same
    ``_normalize_card`` as the Explore feed, so a card shown on
    "my opportunities" is byte-identical in shape (subtitle, location
    label with privacy level honoured, tags, ``detail_path``) to the one
    Explore renders. Because the view only exposes listings that are
    active, not taken down, discovery-eligible and unexpired, an id
    that is not currently viewable is simply absent from the result.

    Deliberately NOT applying viewer exclusions (passed / saved /
    interested): those shape what the *feed* surfaces, not which
    opportunities a user is personally involved with.
    """
    ids = [cid for cid in (safe_uuid(i) for i in listing_ids) if cid]
    if not ids:
        return {}
    out: dict[str, dict[str, Any]] = {}
    chunk_size = 50
    for start in range(0, len(ids), chunk_size):
        chunk = ids[start : start + chunk_size]
        try:
            result = (
                supabase_client.get_service_client()
                .table("discovery_cards")
                .select("*")
                .eq("card_kind", "opportunity")
                .in_("card_id", chunk)
                .execute()
            )
        except Exception:
            logger.exception("unified discovery opportunity-card lookup failed")
            return out
        for raw in getattr(result, "data", None) or []:
            card = _normalize_card(raw, viewer_tags=[])
            if card is not None:
                out[card["card_id"]] = card
    return out


def record_action(
    *,
    user_id: str,
    target_kind: str,
    target_card_id: str,
    action_type: str,
    target_user_id: str | None = None,
) -> bool:
    uid = safe_uuid(user_id)
    cid = safe_uuid(target_card_id)
    kind = clean_kind(target_kind)
    target_uid = safe_uuid(target_user_id) if target_user_id else None
    if (
        not uid
        or not cid
        or kind == "all"
        or action_type not in ALLOWED_ACTIONS
        or (target_uid is not None and target_uid == uid)
    ):
        return False
    try:
        supabase_client.get_service_client().table("creator_discovery_actions").insert(
            {
                "user_id": uid,
                "target_user_id": target_uid,
                "target_kind": kind,
                "target_card_id": cid,
                "action_type": action_type,
            }
        ).execute()
    except Exception:
        logger.exception(
            "unified discovery action failed: %s %s %s", action_type, kind, cid
        )
        return False
    return True


def last_undoable_pass(user_id: str) -> tuple[str, str] | None:
    uid = safe_uuid(user_id)
    if not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_discovery_actions")
            .select("target_kind,target_card_id,action_type,created_at")
            .eq("user_id", uid)
            .in_("action_type", ["passed", "undo_pass"])
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception:
        logger.exception("unified discovery undo lookup failed: %s", user_id)
        return None
    seen: set[tuple[str, str]] = set()
    for row in getattr(result, "data", None) or []:
        key = _row_key(row)
        if key is None or key in seen:
            continue
        seen.add(key)
        if row.get("action_type") == "passed":
            return key
    return None


def clean_kind(value: str | None) -> str:
    candidate = str(value or "all").strip().lower()
    return candidate if candidate in FILTER_KINDS else "all"


def _excluded_card_keys(user_id: str) -> set[tuple[str, str]]:
    cutoff = (datetime.now(UTC) - timedelta(days=PASSED_COOLDOWN_DAYS)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_discovery_actions")
            .select("target_kind,target_card_id,action_type,created_at")
            .eq("user_id", user_id)
            .in_(
                "action_type",
                ["passed", "undo_pass", "saved", "connected", "interested"],
            )
            .order("created_at", desc=True)
            .limit(_EXCLUSION_HISTORY_CAP)
            .execute()
        )
    except Exception:
        logger.exception("unified discovery exclusions failed: %s", user_id)
        return set()

    committed: set[tuple[str, str]] = set()
    latest_pass: dict[tuple[str, str], str] = {}
    latest_undo: dict[tuple[str, str], str] = {}
    for row in getattr(result, "data", None) or []:
        key = _row_key(row)
        if key is None:
            continue
        action = str(row.get("action_type") or "")
        timestamp = str(row.get("created_at") or "")
        if action in {"saved", "connected", "interested"}:
            committed.add(key)
        elif action == "passed" and timestamp >= cutoff:
            latest_pass[key] = max(timestamp, latest_pass.get(key, ""))
        elif action == "undo_pass":
            latest_undo[key] = max(timestamp, latest_undo.get(key, ""))
    standing_passes = {
        key
        for key, timestamp in latest_pass.items()
        if timestamp > latest_undo.get(key, "")
    }
    return committed | standing_passes


def _normalize_card(
    row: dict[str, Any],
    *,
    viewer_tags: list[str],
    viewer_location_label: str | None = None,
    viewer_platform: str | None = None,
) -> dict[str, Any] | None:
    kind = clean_kind(str(row.get("card_kind") or ""))
    card_id = safe_uuid(str(row.get("card_id") or ""))
    owner_id = safe_uuid(str(row.get("owner_user_id") or ""))
    if kind == "all" or not card_id or not owner_id:
        return None
    card = dict(row)
    card["card_kind"] = kind
    card["card_id"] = card_id
    card["owner_user_id"] = owner_id
    card["tags"] = [str(tag) for tag in (card.get("tags") or []) if str(tag).strip()]
    # listing_type is populated only on opportunity rows (migration
    # 0046). Normalize to lowercase string / None so the ordering
    # step never has to think about casing or NaN-y values.
    raw_listing_type = card.get("listing_type")
    if isinstance(raw_listing_type, str) and raw_listing_type.strip():
        card["listing_type"] = raw_listing_type.strip().lower()
    else:
        card["listing_type"] = None
    # Legacy single-string relevance — kept for the detail templates
    # (creator/discover_brand.html, brand/discover_detail.html) that
    # haven't been migrated to the reasons list yet.
    card["why_relevant"] = _why_relevant(card["tags"], viewer_tags, kind)
    # New coral "why babyg picked this" reasons + top-of-card signal
    # badges. Both are lists — empty list = render nothing.
    card["relevance_reasons"] = discover_insights.relevance_reasons(
        card,
        viewer_tags=viewer_tags,
        viewer_location_label=viewer_location_label,
        viewer_platform=viewer_platform,
    )
    card["signal_badges"] = discover_insights.signal_badges(card)
    if kind in {"brand", "opportunity"}:
        card["verification_status"] = brand_trust.clean_status(
            card.get("verification_status")
        )
        card["trust"] = brand_trust.public_trust(card)
    return card


def _why_relevant(card_tags: list[str], viewer_tags: list[str], kind: str) -> str:
    matches = sorted({tag.lower() for tag in card_tags} & {tag.lower() for tag in viewer_tags})
    if matches:
        return f"matches your {', '.join(matches[:2])} focus"
    return {
        "creator": "a creator worth knowing",
        "brand": "a potential brand relationship",
        "opportunity": "a new opportunity for your network",
    }[kind]


def _row_key(row: dict[str, Any]) -> tuple[str, str] | None:
    kind = clean_kind(str(row.get("target_kind") or "creator"))
    card_id = safe_uuid(str(row.get("target_card_id") or row.get("target_user_id") or ""))
    if kind == "all" or not card_id:
        return None
    return kind, card_id
