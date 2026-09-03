"""Autonomy gate for the babyg background agent.

Every action the agent takes on the creator's behalf routes through
`agent_can(user_id, action)`. Read tools skip this check entirely
(reads are always allowed); write tools call it before writing.

Actions are grouped into three families, each backed by one column
on creator_profiles (migration 0034):

  * INTERNAL_ACTIONS -> babyg_agent_internal_actions (default true)
      site-internal state changes: flip a deal stage, rewrite memory,
      mark a draft stale, run sweep bookkeeping. today's sweep
      behavior falls in this family, hence the default of true.

  * GMAIL_AUTO_SEND -> babyg_agent_gmail_auto_send (default false)
      the agent may send a gmail reply directly for narrow, obviously
      safe patterns (acking a confirmed booking, politely declining
      an off-brand pitch). anything ambiguous still stages an
      action_proposals row.

  * CALENDAR_HOLDS -> babyg_agent_calendar_holds (default false)
      the agent may create HOLD events on the creator's own google
      calendar (private visibility, no external invites). never
      sends a real invite without a per-action tap.

Two categories are ALWAYS allowed regardless of setting:

  * PROPOSE       stage an action_proposals row (status='pending').
                  the creator taps to confirm; nothing is sent yet.
  * NUDGE_OR_MEMO drop a nudge into the bot thread, write to
                  bot_messages, snapshot metrics into our own
                  tables. entirely internal, low-blast-radius.

Without those two the agent has no way to surface anything at all,
so gating them by an off-by-default setting would give us a very
expensive scarecrow. The autonomy ladder is about the write blast
radius, not about whether babyg speaks.
"""

from __future__ import annotations

import logging
from typing import Literal

from app.services import profiles

logger = logging.getLogger(__name__)


# Every action name the agent might call agent_can with. Kept in a
# closed set so a typo in a tool implementation surfaces as a KeyError
# in tests rather than a silent grant.
Action = Literal[
    # Always-allowed baseline (agent can always do these).
    "drop_nudge",
    "stage_action_proposal",
    "snapshot_metrics",
    "generate_dm_brief",
    # INTERNAL_ACTIONS family.
    "update_deal_stage",
    "mark_draft_stale",
    "mark_deal_ghosted",
    "rewrite_memory",
    "record_bot_message",
    # GMAIL_AUTO_SEND family.
    "gmail_auto_reply",
    # CALENDAR_HOLDS family.
    "calendar_create_hold",
]

_ALWAYS_ALLOWED: frozenset[str] = frozenset(
    {
        "drop_nudge",
        "stage_action_proposal",
        "snapshot_metrics",
        "generate_dm_brief",
    }
)

_INTERNAL_ACTIONS: frozenset[str] = frozenset(
    {
        "update_deal_stage",
        "mark_draft_stale",
        "mark_deal_ghosted",
        "rewrite_memory",
        "record_bot_message",
    }
)

_GMAIL_AUTO_SEND: frozenset[str] = frozenset({"gmail_auto_reply"})
_CALENDAR_HOLDS: frozenset[str] = frozenset({"calendar_create_hold"})

_KNOWN_ACTIONS: frozenset[str] = (
    _ALWAYS_ALLOWED | _INTERNAL_ACTIONS | _GMAIL_AUTO_SEND | _CALENDAR_HOLDS
)


def agent_can(user_id: str, action: str, *, profile: dict | None = None) -> bool:
    """Return True when the agent may take `action` on `user_id`'s behalf.

    Callers may pass a pre-fetched creator profile via `profile=` to
    avoid a supabase round-trip when they already have one on hand
    (the agent loop loads it once per cycle). When omitted, fetches
    the profile itself.

    Unknown actions are refused with a warning — a typo in a tool
    name must not silently grant permission.
    """
    if action not in _KNOWN_ACTIONS:
        logger.warning("agent_can.unknown_action user=%s action=%s", user_id, action)
        return False
    if action in _ALWAYS_ALLOWED:
        return True
    settings = _load_settings(user_id, profile=profile)
    if action in _INTERNAL_ACTIONS:
        return bool(settings["internal_actions"])
    if action in _GMAIL_AUTO_SEND:
        return bool(settings["gmail_auto_send"])
    if action in _CALENDAR_HOLDS:
        return bool(settings["calendar_holds"])
    return False


def load_settings(user_id: str, *, profile: dict | None = None) -> dict[str, bool]:
    """Public convenience: read the autonomy settings block only."""
    return _load_settings(user_id, profile=profile)


def _load_settings(user_id: str, *, profile: dict | None = None) -> dict[str, bool]:
    if profile is None:
        profile = profiles.get_creator_profile(user_id) or {}
    return {
        # internal_actions defaults to True to preserve today's sweep
        # behavior on any creator whose row predates migration 0034
        # (the migration sets it to true, but be defensive on read).
        "internal_actions": _bool(profile.get("babyg_agent_internal_actions"), True),
        "gmail_auto_send": _bool(profile.get("babyg_agent_gmail_auto_send"), False),
        "calendar_holds": _bool(profile.get("babyg_agent_calendar_holds"), False),
    }


def _bool(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return default
