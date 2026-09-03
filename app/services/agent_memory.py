"""Rolling creator summary for the babyg background agent.

The agent maintains a long-form prose model of the creator and
their world. It's loaded into every agent prompt (that's what makes
babyg feel like it remembers you), and rewritten by the agent when
new info is worth committing. The creator can also edit it directly
from /creator/profile/settings.

Public shape:

    load(user_id)                             -> dict | None
    save(user_id, summary, *, updated_by,     -> dict | None
         change_reason=None)
    history(user_id, limit=20)                -> list[dict]
    SUMMARY_MAX_CHARS                          hard cap on summary length

Every save reads the current row, bumps the version, replaces it,
and appends a history row. history is append-only from the service
layer; row-level policy blocks writes from a user session, so a
user can only *edit* through save() (which stamps updated_by='user'
and records the history entry alongside).

Failure semantics: load/history return None/[] on any supabase
error and log; save returns None (the caller should not treat
this as "the memory was persisted"). Never raises.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from app.core import supabase_client

logger = logging.getLogger(__name__)

# Hard ceiling on the summary. It's loaded into every agent prompt
# so unbounded growth is a direct token-cost multiplier. ~2000
# tokens is enough for a rich prose model of a creator.
SUMMARY_MAX_CHARS = 8_000
CHANGE_REASON_MAX_CHARS = 500

UpdatedBy = Literal["agent", "user"]


def load(user_id: str) -> dict[str, Any] | None:
    """Return the current memory row for this creator, or None."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_agent_memory")
            .select("user_id,summary,version,updated_by,updated_at")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("agent_memory.load.read_failed user=%s", user_id)
        return None
    rows = list(getattr(result, "data", None) or [])
    return rows[0] if rows else None


def save(
    user_id: str,
    summary: str,
    *,
    updated_by: UpdatedBy,
    change_reason: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Persist a new summary, incrementing version and writing history.

    The write is two atomic-ish steps: upsert current, insert history.
    We treat them as best-effort — a history write failure logs but
    does not roll back the current-state upsert, because the current
    row is what the agent loads next cycle. A missing history entry
    is a legibility loss, not a correctness one.
    """
    if updated_by not in ("agent", "user"):
        logger.warning(
            "agent_memory.bad_updated_by user=%s value=%s", user_id, updated_by
        )
        return None
    cleaned = (summary or "").strip()[:SUMMARY_MAX_CHARS]
    reason_clean = (change_reason or "").strip()[:CHANGE_REASON_MAX_CHARS] or None
    current = load(user_id) or {}
    next_version = int(current.get("version") or 0) + 1
    ts = (now or datetime.now(UTC)).isoformat()
    body = {
        "user_id": user_id,
        "summary": cleaned,
        "version": next_version,
        "updated_by": updated_by,
        "updated_at": ts,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_agent_memory")
            .upsert(body, on_conflict="user_id")
            .execute()
        )
    except Exception:
        logger.exception("agent_memory.save.write_failed user=%s", user_id)
        return None
    rows = list(getattr(result, "data", None) or [])
    saved = rows[0] if rows else body

    history_row = {
        "user_id": user_id,
        "version": next_version,
        "summary": cleaned,
        "updated_by": updated_by,
        "change_reason": reason_clean,
        "created_at": ts,
    }
    try:
        (
            supabase_client.get_service_client()
            .table("creator_agent_memory_history")
            .insert(history_row)
            .execute()
        )
    except Exception:
        logger.exception(
            "agent_memory.save.history_failed user=%s version=%s",
            user_id,
            next_version,
        )
    return saved


def history(user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit), 200))
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_agent_memory_history")
            .select("id,version,summary,updated_by,change_reason,created_at")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception("agent_memory.history.read_failed user=%s", user_id)
        return []
    return list(getattr(result, "data", None) or [])
