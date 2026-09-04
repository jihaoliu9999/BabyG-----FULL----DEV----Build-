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

from postgrest.types import CountMethod

from app.core import supabase_client
from app.integrations import google_calendar, google_gmail
from app.services import (
    agent_autonomy,
    agent_memory,
    agent_safety,
    babyg_deals,
    babyg_memory,
    bot,
    oauth_connections,
)

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
    # bot.create_message returns the inserted row's id as a plain str (or
    # None on failure), not a dict. Pass it through directly.
    return {"ok": True, "message_id": message}


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


def gmail_auto_reply(
    user_id: str,
    *,
    thread_id: str,
    to: str,
    subject: str,
    body: str,
    profile: dict | None = None,
) -> dict[str, Any]:
    """Send one gmail reply autonomously. Requires GMAIL_AUTO_SEND
    autonomy AND passes agent_safety.is_gmail_reply_safe.

    Double-gated: autonomy setting says "allowed at all"; safety
    classifier says "this specific message is boring enough to trust."
    A creator with GMAIL_AUTO_SEND=True still gets refusals on any
    reply that mentions money, urls, phone numbers, committal language,
    or is missing a thread_id (no first-touch sends, ever).
    """
    if not agent_autonomy.agent_can(user_id, "gmail_auto_reply", profile=profile):
        return {
            "ok": False,
            "reason": "autonomy_denied",
            "action": "gmail_auto_reply",
        }
    safe, safety_reason = agent_safety.is_gmail_reply_safe(
        thread_id=thread_id, subject=subject, body=body
    )
    if not safe:
        return {"ok": False, "reason": "unsafe_content", "detail": safety_reason}
    try:
        token = oauth_connections.access_token_for_google(user_id)
    except Exception:
        logger.exception("agent_writes.gmail_auto_reply.token_failed user=%s", user_id)
        return {"ok": False, "reason": "token_lookup_failed"}
    if not token:
        return {"ok": False, "reason": "no_gmail_token"}
    try:
        message_id = google_gmail.send_message(
            token,
            to=to,
            subject=subject,
            body=body,
            thread_id=thread_id,
        )
    except google_gmail.GmailError as exc:
        logger.warning(
            "agent_writes.gmail_auto_reply.send_failed user=%s error=%s",
            user_id,
            exc,
        )
        return {"ok": False, "reason": "gmail_send_failed", "detail": str(exc)[:200]}
    except Exception:
        logger.exception("agent_writes.gmail_auto_reply.send_crashed user=%s", user_id)
        return {"ok": False, "reason": "gmail_send_failed"}
    return {
        "ok": True,
        "message_id": message_id,
        "kind": agent_safety.classify_reply(body),
    }


def calendar_create_hold(
    user_id: str,
    *,
    title: str,
    starts_at: str,
    ends_at: str,
    notes: str | None = None,
    profile: dict | None = None,
) -> dict[str, Any]:
    """Put a HOLD event on the creator's own google calendar. Requires
    CALENDAR_HOLDS autonomy.

    HOLD semantics: visibility=private (invisible to anyone the
    creator shares their calendar with), transparency=opaque (blocks
    time as busy so other tooling sees the reservation). Never invites
    external attendees — that requires an explicit per-action tap.
    """
    if not agent_autonomy.agent_can(user_id, "calendar_create_hold", profile=profile):
        return {
            "ok": False,
            "reason": "autonomy_denied",
            "action": "calendar_create_hold",
        }
    try:
        token = oauth_connections.access_token_for_google(user_id)
    except Exception:
        logger.exception(
            "agent_writes.calendar_create_hold.token_failed user=%s", user_id
        )
        return {"ok": False, "reason": "token_lookup_failed"}
    if not token:
        return {"ok": False, "reason": "no_calendar_token"}
    try:
        event_id = google_calendar.create_primary_event(
            token,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            notes=notes,
            visibility="private",
            transparency="opaque",
        )
    except google_calendar.GoogleCalendarError as exc:
        logger.warning(
            "agent_writes.calendar_create_hold.insert_failed user=%s error=%s",
            user_id,
            exc,
        )
        return {"ok": False, "reason": "calendar_insert_failed", "detail": str(exc)[:200]}
    except Exception:
        logger.exception(
            "agent_writes.calendar_create_hold.crashed user=%s", user_id
        )
        return {"ok": False, "reason": "calendar_insert_failed"}
    return {"ok": True, "event_id": event_id}


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
            .select("id", count=CountMethod.exact)
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
