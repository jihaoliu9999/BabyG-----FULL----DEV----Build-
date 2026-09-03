"""Read-only tools the babyg background agent uses to observe the world.

Every tool here is pure read. No writes, no side effects — those live
in app/services/agent_writes.py (coming in #6) and are gated by
agent_autonomy.

The agent loop uses these in two ways:

1. **Pre-filter (heuristic, no LLM):** `observe(user_id)` runs every
   cycle and returns a structured snapshot. If nothing meaningful
   changed since the last cycle, the loop short-circuits with status
   'skipped_no_delta' and no claude call is made. This is what keeps
   the tight $0.10/creator/day cap workable.

2. **In-loop tool call (LLM-driven):** the individual tools
   (`stale_draft_candidates`, `ghosted_deal_candidates`, etc.) are
   surfaced to claude as tool definitions. When the LLM needs more
   detail on one dimension, it invokes the tool and reasons over
   the result.

Contract for every tool:
- Signature: `tool(user_id, ..., limit=N)`.
- Return: JSON-serializable dict or list of dicts.
- Never raises. Failure -> empty list / empty dict / logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import supabase_client
from app.services import action_proposals, bookings, dms

logger = logging.getLogger(__name__)

# Kept in sync with bot_jobs.STALE_DRAFT_DAYS / GHOSTED_DEAL_DAYS.
# Duplicated here on purpose: agent_tools is the read facade the
# agent loop uses; bot_jobs is the write path the current sweeps
# use. When we cut over in #8, the sweeps will call these instead,
# and there'll be one canonical constant.
STALE_DRAFT_DAYS = 14
GHOSTED_DEAL_DAYS = 14

# Deal stages that are still "working" — same set the ghosted-deals
# sweep uses. Terminal + payment_pending stages are excluded because
# they're waiting on the money, not the brand.
_WORKING_STAGES: frozenset[str] = frozenset(
    {"discovery", "pitching", "negotiating", "drafting", "in_progress"}
)

_MAX_LIMIT = 50


def _service():
    return supabase_client.get_service_client()


def stale_draft_candidates(
    user_id: str,
    *,
    now: datetime | None = None,
    days: int = STALE_DRAFT_DAYS,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Drafts still in 'proposed' or 'edited' that have been sitting
    for at least `days` days. Doesn't touch them — just returns the
    rows the sweep or agent would flip if it decided to.
    """
    capped = max(1, min(int(limit), _MAX_LIMIT))
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    try:
        result = (
            _service()
            .table("babyg_memory_drafts")
            .select("id,creator_id,updated_at,status,brand_name,deal_id")
            .eq("creator_id", user_id)
            .in_("status", ["proposed", "edited"])
            .lte("updated_at", cutoff.isoformat())
            .order("updated_at", desc=True)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception("agent_tools.stale_draft_candidates.failed user=%s", user_id)
        return []
    return list(getattr(result, "data", None) or [])


def ghosted_deal_candidates(
    user_id: str,
    *,
    now: datetime | None = None,
    days: int = GHOSTED_DEAL_DAYS,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Working-stage deals with no touch in `days` days. Terminal +
    payment_pending stages are excluded."""
    capped = max(1, min(int(limit), _MAX_LIMIT))
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=days)
    try:
        result = (
            _service()
            .table("babyg_memory_deals")
            .select("id,creator_id,brand_name,stage,last_touch_at")
            .eq("creator_id", user_id)
            .in_("stage", sorted(_WORKING_STAGES))
            .lte("last_touch_at", cutoff.isoformat())
            .order("last_touch_at", desc=True)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception(
            "agent_tools.ghosted_deal_candidates.failed user=%s", user_id
        )
        return []
    return list(getattr(result, "data", None) or [])


def upcoming_bookings(user_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    try:
        rows = bookings.list_for_user(user_id, horizon="upcoming", limit=limit)
    except Exception:
        logger.exception("agent_tools.upcoming_bookings.failed user=%s", user_id)
        return []
    # Return a slim projection to keep the agent prompt lean.
    slim = []
    for b in rows or []:
        slim.append(
            {
                "id": b.get("id"),
                "title": b.get("title"),
                "starts_at": b.get("starts_at"),
                "ends_at": b.get("ends_at"),
                "venue_name": b.get("venue_name"),
                "status": b.get("status"),
            }
        )
    return slim


def unread_dms_snapshot(user_id: str) -> dict[str, Any]:
    """Compact 'how many unread across whom' for the agent's pre-filter.
    Doesn't return message bodies — that's a per-thread read tool."""
    try:
        count = int(dms.unread_count_for_user(user_id) or 0)
    except Exception:
        logger.exception("agent_tools.unread_dms_snapshot.failed user=%s", user_id)
        count = 0
    return {"count": count}


def pending_action_proposals_snapshot(user_id: str) -> dict[str, Any]:
    """How many babyg-staged proposals the creator hasn't touched yet.
    Used by the agent to decide 'do i need to bump anything?'"""
    try:
        rows = action_proposals.list_pending_for_user(user_id=user_id, limit=50)
    except Exception:
        logger.exception(
            "agent_tools.pending_action_proposals_snapshot.failed user=%s", user_id
        )
        rows = []
    by_kind: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("action_type") or "unknown")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return {"count": len(rows), "by_action_type": by_kind}


def observe(user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """Aggregate snapshot the agent's pre-filter reads every cycle.

    Structured so the caller can compute a cheap delta_summary
    ("nothing new / 3 aging deals + 1 new dm / etc.") without any
    LLM call. When the summary is entirely empty, the loop
    short-circuits with status='skipped_no_delta' and no tokens are
    spent this cycle.
    """
    now = now or datetime.now(UTC)
    return {
        "as_of": now.astimezone(UTC).isoformat(),
        "stale_drafts": stale_draft_candidates(user_id, now=now, limit=20),
        "ghosted_deals": ghosted_deal_candidates(user_id, now=now, limit=20),
        "upcoming_bookings": upcoming_bookings(user_id, limit=5),
        "unread_dms": unread_dms_snapshot(user_id),
        "pending_action_proposals": pending_action_proposals_snapshot(user_id),
    }


def delta_summary(snapshot: dict[str, Any]) -> dict[str, int]:
    """Cheap 'is anything new?' derivation from an observe() snapshot.

    Returns a dict of int counts across dimensions. If the sum is 0
    the loop can skip this cycle entirely. Keeps the pre-filter
    ordering explicit (documented here rather than baked into the
    loop file, so future dimensions add themselves here).
    """
    return {
        "stale_drafts": len(snapshot.get("stale_drafts") or []),
        "ghosted_deals": len(snapshot.get("ghosted_deals") or []),
        "upcoming_bookings": len(snapshot.get("upcoming_bookings") or []),
        "unread_dms": int((snapshot.get("unread_dms") or {}).get("count") or 0),
        "pending_action_proposals": int(
            (snapshot.get("pending_action_proposals") or {}).get("count") or 0
        ),
    }
