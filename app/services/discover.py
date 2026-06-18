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
    limit: int = DEFAULT_LIMIT,
    prioritize: tuple[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Return a filtered, public-safe mixed card stack.

    The view owns the cross-table projection. This function only applies
    viewer-specific filters and action-history exclusions.
    """
    uid = safe_uuid(viewer_id)
    if not uid or viewer_role not in {"creator", "brand"}:
        return []
    kind_clean = clean_kind(kind)
    bounded = max(1, min(int(limit or DEFAULT_LIMIT), HARD_LIMIT))

    try:
        query = (
            supabase_client.get_service_client()
            .table("discovery_cards")
            .select("*")
            .neq("owner_user_id", uid)
            .order("created_at", desc=True)
            .limit(HARD_LIMIT * 4)
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
        card = _normalize_card(raw, viewer_tags=viewer_tags or [])
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
    return normalized[:bounded]


def get_card(*, card_kind: str, card_id: str) -> dict[str, Any] | None:
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
    return _normalize_card(rows[0], viewer_tags=[]) if rows else None


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
    row: dict[str, Any], *, viewer_tags: list[str]
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
    card["why_relevant"] = _why_relevant(card["tags"], viewer_tags, kind)
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
