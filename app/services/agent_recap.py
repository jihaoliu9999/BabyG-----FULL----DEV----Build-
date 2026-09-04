"""'Since you were away' recap for the home dashboard.

Answers the specific question a creator asks when they open babyg
first thing in the morning: what did babyg actually do overnight?

The recap is a read-only aggregate — no writes, no side effects. It
scans three tables for the last N hours (default 12) and shapes a
compact summary the home template can render at the top:

  - action proposals staged for tap-to-approve
  - agent cycles that actually ran tools (not skipped)
  - agent-authored nudges dropped into the bot thread
  - agent memory rewrites

Empty state: build() returns None. The template hides the card
entirely when there's nothing to say — silence is the correct
"nothing happened overnight" affordance, not a "nothing to see"
empty state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import supabase_client

logger = logging.getLogger(__name__)

DEFAULT_WINDOW_HOURS = 12
_MAX_HEADLINES = 6


def _service():
    return supabase_client.get_service_client()


def build(
    user_id: str,
    *,
    window_hours: int = DEFAULT_WINDOW_HOURS,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return a recap dict, or None when nothing happened in the window.

    Shape:
      {
        "window_hours": 12,
        "since": "2026-09-03T22:00:00Z",
        "counts": {
          "proposals": 2,       staged action_proposals awaiting tap
          "cycles_active": 1,   agent cycles that ran ANY tool
          "nudges": 3,          agent-authored bot_messages dropped
          "memory_writes": 1,   agent memory rewrites
        },
        "headlines": ["drafted 2 replies", "flipped 1 stale deal", ...]
      }

    Every read is wrapped: any single failure defaults to 0 for that
    dimension, so a supabase blip on one table doesn't blank the
    whole recap.
    """
    now = now or datetime.now(UTC)
    since = now - timedelta(hours=max(1, int(window_hours)))
    since_iso = since.isoformat()

    counts = {
        "proposals": _count_proposals_since(user_id, since_iso),
        "cycles_active": _count_active_cycles_since(user_id, since_iso),
        "nudges": _count_agent_nudges_since(user_id, since_iso),
        "memory_writes": _count_memory_writes_since(user_id, since_iso),
    }
    if sum(counts.values()) == 0:
        return None

    headlines = _headlines(counts)
    return {
        "window_hours": int(window_hours),
        "since": since_iso,
        "counts": counts,
        "headlines": headlines[:_MAX_HEADLINES],
    }


def _headlines(counts: dict[str, int]) -> list[str]:
    """Turn raw counts into short lowercase lines the template renders
    verbatim. Ordering matters — most-actionable first."""
    out: list[str] = []
    if counts["proposals"] > 0:
        out.append(
            f"staged {counts['proposals']} action"
            f"{'s' if counts['proposals'] != 1 else ''} for your tap"
        )
    if counts["nudges"] > 0:
        out.append(
            f"dropped {counts['nudges']} nudge"
            f"{'s' if counts['nudges'] != 1 else ''} in your bot thread"
        )
    if counts["cycles_active"] > 0:
        out.append(
            f"ran {counts['cycles_active']} thinking cycle"
            f"{'s' if counts['cycles_active'] != 1 else ''}"
        )
    if counts["memory_writes"] > 0:
        out.append(
            f"updated your memory {counts['memory_writes']} time"
            f"{'s' if counts['memory_writes'] != 1 else ''}"
        )
    return out


def _count_proposals_since(user_id: str, since_iso: str) -> int:
    try:
        result = (
            _service()
            .table("action_proposals")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .gte("created_at", since_iso)
            .execute()
        )
    except Exception:
        logger.exception("agent_recap.proposals.read_failed user=%s", user_id)
        return 0
    return int(getattr(result, "count", None) or 0)


def _count_active_cycles_since(user_id: str, since_iso: str) -> int:
    """Only 'ok' cycles that actually did something count as 'active'.
    Skipped cycles (no delta, over cap) don't need to be recapped."""
    try:
        result = (
            _service()
            .table("agent_cycles")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("status", "ok")
            .gte("cycle_started_at", since_iso)
            .execute()
        )
    except Exception:
        logger.exception("agent_recap.cycles.read_failed user=%s", user_id)
        return 0
    return int(getattr(result, "count", None) or 0)


def _count_agent_nudges_since(user_id: str, since_iso: str) -> int:
    try:
        result = (
            _service()
            .table("bot_messages")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("role", "assistant")
            .gte("created_at", since_iso)
            .like("tool_calls->>source", "agent%")
            .execute()
        )
    except Exception:
        logger.exception("agent_recap.nudges.read_failed user=%s", user_id)
        return 0
    return int(getattr(result, "count", None) or 0)


def _count_memory_writes_since(user_id: str, since_iso: str) -> int:
    try:
        result = (
            _service()
            .table("creator_agent_memory_history")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("updated_by", "agent")
            .gte("created_at", since_iso)
            .execute()
        )
    except Exception:
        logger.exception("agent_recap.memory.read_failed user=%s", user_id)
        return 0
    return int(getattr(result, "count", None) or 0)
