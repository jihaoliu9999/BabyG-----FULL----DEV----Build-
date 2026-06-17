"""Swipe-style creator discovery — the stack-ranking + action recorder.

The network page is a one-card-at-a-time deck. This module is the
source of truth for two things:

  * `next_stack_for(user_id, limit)` — onboarded creators the viewer
    hasn't passed-recently or connected-with, minus self and blocked.
    Order: most-recently-onboarded first.
  * `record_action(user_id, target_id, action_type)` — append-only
    insert into `creator_discovery_actions`. Each pass earns the
    target a 30-day cooldown from the viewer's stack; each connect
    or open_profile is permanent.

Privacy:
  * Discovery history is private. No projection of this data flows
    to the targeted creator.
  * Targets are public-projected via `profiles.public_creator` so
    private fields never reach the swipe template.

Defensive shape: any storage failure returns an empty stack or False
rather than raising — keeps the page rendering an empty state instead
of crashing on a Supabase outage.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Final, Literal

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import network, profiles

logger = logging.getLogger(__name__)

ActionType = Literal[
    "viewed", "passed", "connected", "skipped", "opened_profile", "undo_pass"
]
ALLOWED_ACTIONS: Final[frozenset[str]] = frozenset(
    {"viewed", "passed", "connected", "skipped", "opened_profile", "undo_pass"}
)

# How long a "passed" creator stays out of the viewer's stack. Not
# permanent — gives the viewer a chance to re-encounter someone they
# passed on a bad day. Connected / opened_profile are permanent.
PASSED_COOLDOWN_DAYS: Final = 30

# Soft cap on a single discovery_stack call. The route renders one
# card at a time but pre-loads a small batch into the DOM for instant
# swipes. A bigger limit would be wasted bandwidth.
DEFAULT_STACK_LIMIT: Final = 8
HARD_STACK_LIMIT: Final = 25


def record_action(
    *, user_id: str, target_user_id: str, action_type: str
) -> bool:
    """Append one discovery action. Returns True on success, False on
    any validation or storage failure — caller is expected to render
    the next card either way (the user already swiped)."""
    if action_type not in ALLOWED_ACTIONS:
        return False
    user_safe = safe_uuid(user_id)
    target_safe = safe_uuid(target_user_id)
    if not user_safe or not target_safe or user_safe == target_safe:
        return False
    try:
        supabase_client.get_service_client().table(
            "creator_discovery_actions"
        ).insert(
            {
                "user_id": user_safe,
                "target_user_id": target_safe,
                "action_type": action_type,
            }
        ).execute()
    except Exception:
        logger.exception(
            "discovery action insert failed: %s %s -> %s",
            action_type,
            user_id,
            target_user_id,
        )
        return False
    return True


def next_stack_for(
    user_id: str,
    *,
    limit: int = DEFAULT_STACK_LIMIT,
    prioritize_user_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return the next batch of creators the viewer hasn't acted on.

    Exclusions (in order of cost):
      1. Self.
      2. Anyone in a blocked or accepted relationship with the viewer
         (in either direction) — uses `network._list_for_user` for the
         accepted side and `network._blocked_user_ids` for blocks.
      3. Anyone the viewer has an outstanding pending connection with.
      4. Anyone the viewer "passed" within the cooldown window.
      5. Anyone the viewer has "connected" or "opened_profile" on.
         (Connected lands permanent because they're in the connections
         flow already; opened_profile is permanent because the viewer
         has already committed attention.)

    Returns public-projected creator dicts via `profiles.public_creator`
    — private fields are stripped at this boundary.
    """
    bounded = max(1, min(int(limit or DEFAULT_STACK_LIMIT), HARD_STACK_LIMIT))

    excluded = _excluded_user_ids(user_id)
    excluded.add(user_id)
    if prioritize_user_id:
        # The viewer just undid a pass on this creator — make sure the
        # pass exclusion doesn't keep them hidden, and float them to the
        # front so the undo lands them back on the active card.
        excluded.discard(prioritize_user_id)

    # The base set: onboarded creators, ordered newest first. The
    # existing network helper applies the privacy projection at the
    # row level so we don't have to re-project.
    candidates = network._list_onboarded_creators()
    if prioritize_user_id:
        candidates = sorted(
            candidates,
            key=lambda r: str(r.get("user_id") or "") != prioritize_user_id,
        )
    out: list[dict[str, Any]] = []
    for row in candidates:
        target_id = str(row.get("user_id") or "")
        if not target_id or target_id in excluded:
            continue
        projected = profiles.public_creator(row)
        if projected is None:
            continue
        out.append(projected)
        if len(out) >= bounded:
            break
    return out


def _excluded_user_ids(user_id: str) -> set[str]:
    """Union of: blocked, accepted-or-pending connections (either
    direction), passed-within-cooldown, connected / opened_profile in
    discovery_actions.

    Returns ids as strings to match `row.get("user_id")` comparisons
    in `next_stack_for`.
    """
    excluded: set[str] = set()

    # Blocked + accepted/pending connection peers — covers the existing
    # creator_connections table. blocked is already wrapped in network.
    excluded.update(network._blocked_user_ids(user_id))
    excluded.update(_connected_or_pending_peer_ids(user_id))

    # Discovery-side exclusions.
    excluded.update(_recently_passed_target_ids(user_id))
    excluded.update(_committed_target_ids(user_id))

    return excluded


def _connected_or_pending_peer_ids(user_id: str) -> set[str]:
    """Anyone the viewer is already connected with (accepted), has a
    pending outgoing request to, or has disconnected from (removed). We
    DON'T exclude incoming pending — the viewer might still want to see
    that creator and respond via the full profile.

    `removed` (a torn-down connection) is excluded too: a disconnected
    creator should not instantly reappear in the swipe stack."""
    uid = safe_uuid(user_id)
    if not uid:
        return set()
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_connections")
            .select("requester_id, addressee_id, status")
            .or_(f"requester_id.eq.{uid},addressee_id.eq.{uid}")
            .in_("status", ["pending", "accepted", "removed"])
            .execute()
        )
    except Exception:
        logger.exception("discovery connection exclusion lookup failed: %s", user_id)
        return set()
    rows = getattr(result, "data", None) or []
    out: set[str] = set()
    for row in rows:
        requester = str(row.get("requester_id") or "")
        addressee = str(row.get("addressee_id") or "")
        status = str(row.get("status") or "")
        if status in ("accepted", "removed"):
            # Connected, or connected-then-disconnected — exclude the peer
            # in both directions so they don't resurface in the stack.
            peer = addressee if requester == user_id else requester
            if peer:
                out.add(peer)
        elif status == "pending":
            # Only exclude when WE are the requester (outgoing pending).
            # Incoming pending stays visible so the viewer can act.
            if requester == user_id and addressee:
                out.add(addressee)
    return out


def _recently_passed_target_ids(user_id: str) -> set[str]:
    """Targets the viewer passed on within the cooldown window, minus
    any the viewer has since undone.

    A creator stays excluded only while their most recent pass/undo
    action is a `passed`. If an `undo_pass` is more recent than the
    latest `passed`, the creator is restored to the stack immediately.
    """
    uid = safe_uuid(user_id)
    if not uid:
        return set()
    cutoff = (datetime.now(UTC) - timedelta(days=PASSED_COOLDOWN_DAYS)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_discovery_actions")
            .select("target_user_id, action_type, created_at")
            .eq("user_id", uid)
            .in_("action_type", ["passed", "undo_pass"])
            .gte("created_at", cutoff)
            .execute()
        )
    except Exception:
        logger.exception("recently-passed lookup failed: %s", user_id)
        return set()
    rows = getattr(result, "data", None) or []
    latest_passed: dict[str, str] = {}
    latest_undo: dict[str, str] = {}
    for r in rows:
        target = str(r.get("target_user_id") or "")
        if not target:
            continue
        ts = str(r.get("created_at") or "")
        if r.get("action_type") == "passed" and ts > latest_passed.get(target, ""):
            latest_passed[target] = ts
        elif r.get("action_type") == "undo_pass" and ts > latest_undo.get(target, ""):
            latest_undo[target] = ts
    return {
        target
        for target, passed_ts in latest_passed.items()
        if passed_ts > latest_undo.get(target, "")
    }


def last_undoable_pass(user_id: str) -> str | None:
    """The most recently passed creator the viewer can still undo —
    i.e. whose latest pass/undo action is a `passed`. Returns the
    target_user_id, or None when there is nothing to undo."""
    uid = safe_uuid(user_id)
    if not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_discovery_actions")
            .select("target_user_id, action_type, created_at")
            .eq("user_id", uid)
            .in_("action_type", ["passed", "undo_pass"])
            .order("created_at", desc=True)
            .limit(200)
            .execute()
        )
    except Exception:
        logger.exception("last-undoable-pass lookup failed: %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    # Walk newest-first; the first time we meet a target decides its
    # current state. A leading `passed` is a standing (undoable) pass;
    # a leading `undo_pass` means it's already been restored.
    seen: set[str] = set()
    for r in rows:
        target = str(r.get("target_user_id") or "")
        if not target or target in seen:
            continue
        seen.add(target)
        if r.get("action_type") == "passed":
            return target
    return None


def _committed_target_ids(user_id: str) -> set[str]:
    """Targets the viewer has connected with or opened-profile on, ever.
    Both are treated as committed attention — they don't reappear in
    the swipe stack."""
    uid = safe_uuid(user_id)
    if not uid:
        return set()
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_discovery_actions")
            .select("target_user_id")
            .eq("user_id", uid)
            .in_("action_type", ["connected", "opened_profile"])
            .execute()
        )
    except Exception:
        logger.exception("committed-target lookup failed: %s", user_id)
        return set()
    rows = getattr(result, "data", None) or []
    return {str(r.get("target_user_id") or "") for r in rows if r.get("target_user_id")}
