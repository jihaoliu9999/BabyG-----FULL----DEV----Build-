"""Write tools for the babyg background agent loop.

Every function here is a side-effectful step the loop can take on
the creator's behalf. Each one:

  1. Gates on agent_autonomy.agent_can before touching anything.
  2. Returns a structured dict {ok, reason, ...} the loop can log
     into agent_cycles.tools_called.
  3. Never raises — a failed write becomes {"ok": False, ...} so
     one bad tool call doesn't halt the whole cycle.

Two extra rules:

  * drop_nudge is rate-capped. even though the "chattiness" setting
    says immediate, if the loop produces 10 findings in one cycle
    that's a firehose. NUDGE_RATE_CAP_PER_HOUR caps to 4/hour; the
    5th and beyond are collapsed into a single "N more, tap to see"
    row the agent surfaces at cycle end.

  * rewrite_memory always writes both current-state and history.
    it's agent_memory.save with updated_by='agent' — the audit
    trail is the whole point of the memory table.

The loop wires each of these into a claude tool definition. The
autonomy check happens on the server side of the tool call, not
in claude — a creator with a locked-down autonomy setting can
tell claude "please auto-send that reply" and the server will
still refuse.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import supabase_client
from app.services import agent_autonomy, agent_memory, babyg_deals, babyg_memory, bot

logger = logging.getLogger(__name__)

# Soft cap. hard cap of 10 in case the rate-count read returns bad
# data; nothing gets past that even under RLS-broken conditions.
NUDGE_RATE_CAP_PER_HOUR = 4
NUDGE_HARD_CAP_PER_HOUR = 10
NUDGE_RATE_WINDOW_MINUTES = 60

_NUDGE_SOURCE_PREFIX = "agent"


def drop_nudge(
    user_id: str,
    *,
    body: str,
    category: str,
    chips: list[dict[str, Any]] | None = None,
    profile: dict | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Insert one nudge into the creator's bot thread.

    Baseline capability (always allowed by autonomy). Rate-capped:
    the 5th nudge within a rolling 60-min window is refused with
    reason='rate_capped', so the caller can collapse the tail into
    one summary nudge instead of firing 12 separately.
    """
    if not agent_autonomy.agent_can(user_id, "drop_nudge", profile=profile):
        return {"ok": False, "reason": "autonomy_denied", "action": "drop_nudge"}
    n_recent = _count_recent_agent_nudges(user_id, now=now)
    if n_recent >= NUDGE_HARD_CAP_PER_HOUR:
        return {"ok": False, "reason": "rate_capped_hard", "count": n_recent}
    if n_recent >= NUDGE_RATE_CAP_PER_HOUR:
        return {"ok": False, "reason": "rate_capped", "count": n_recent}
    tool_calls: dict[str, Any] = {
        "source": f"{_NUDGE_SOURCE_PREFIX}_loop",
        "kind": "nudge",
        "nudge_category": category,
    }
    if chips:
        tool_calls["chips"] = chips
    try:
        message = bot.create_message(
            user_id=user_id,
            role="assistant",
            content=body,
            tool_calls=tool_calls,
        )
    except Exception:
        logger.exception("agent_writes.drop_nudge.write_failed user=%s", user_id)
        return {"ok": False, "reason": "write_failed"}
    return {"ok": True, "message_id": (message or {}).get("id")}


def rewrite_memory(
    user_id: str,
    summary: str,
    *,
    change_reason: str,
    profile: dict | None = None,
) -> dict[str, Any]:
    """Persist a new memory summary. Requires INTERNAL_ACTIONS."""
    if not agent_autonomy.agent_can(user_id, "rewrite_memory", profile=profile):
        return {"ok": False, "reason": "autonomy_denied", "action": "rewrite_memory"}
    saved = agent_memory.save(
        user_id, summary, updated_by="agent", change_reason=change_reason
    )
    if saved is None:
        return {"ok": False, "reason": "write_failed"}
    return {"ok": True, "version": saved.get("version")}


def update_deal_stage(
    user_id: str,
    deal_id: str,
    to_stage: str,
    *,
    profile: dict | None = None,
) -> dict[str, Any]:
    """Flip a deal to a new stage. Requires INTERNAL_ACTIONS."""
    if not agent_autonomy.agent_can(user_id, "update_deal_stage", profile=profile):
        return {"ok": False, "reason": "autonomy_denied", "action": "update_deal_stage"}
    try:
        out = babyg_deals.update_stage(deal_id, to_stage, creator_id=user_id)
    except Exception:
        logger.exception("agent_writes.update_deal_stage.failed user=%s deal=%s", user_id, deal_id)
        return {"ok": False, "reason": "write_failed"}
    if out is None:
        return {"ok": False, "reason": "refused"}
    return {"ok": True, "deal_id": deal_id, "to_stage": to_stage}


def mark_draft_stale(
    user_id: str,
    draft_id: str,
    *,
    profile: dict | None = None,
) -> dict[str, Any]:
    """Flip a draft to 'stale'. Requires INTERNAL_ACTIONS."""
    if not agent_autonomy.agent_can(user_id, "mark_draft_stale", profile=profile):
        return {"ok": False, "reason": "autonomy_denied", "action": "mark_draft_stale"}
    try:
        ok = babyg_memory.update_draft_status(draft_id, "stale")
    except Exception:
        logger.exception(
            "agent_writes.mark_draft_stale.failed user=%s draft=%s", user_id, draft_id
        )
        return {"ok": False, "reason": "write_failed"}
    if not ok:
        return {"ok": False, "reason": "write_failed"}
    return {"ok": True, "draft_id": draft_id}


def _count_recent_agent_nudges(
    user_id: str, *, now: datetime | None = None
) -> int:
    """How many nudges the agent has dropped in the last hour.

    Filters bot_messages by (user_id, role='assistant',
    tool_calls->>'source' LIKE 'agent%', created_at > cutoff).
    A read failure returns 0 — better to overshoot the rate cap
    once than to blank the loop's ability to speak.
    """
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(minutes=NUDGE_RATE_WINDOW_MINUTES)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("bot_messages")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("role", "assistant")
            .gte("created_at", cutoff)
            .like("tool_calls->>source", f"{_NUDGE_SOURCE_PREFIX}%")
            .execute()
        )
    except Exception:
        logger.exception(
            "agent_writes.count_recent_nudges.read_failed user=%s", user_id
        )
        return 0
    return int(getattr(result, "count", None) or 0)


def new_idempotency_key(*parts: str) -> str:
    """Shape a stable idempotency key from tool-call parts. Prevents
    the loop from stacking duplicate action_proposals if it re-runs
    on the same delta twice."""
    payload = ":".join(str(p) for p in parts if p)
    return str(uuid.uuid5(uuid.NAMESPACE_URL, payload))
