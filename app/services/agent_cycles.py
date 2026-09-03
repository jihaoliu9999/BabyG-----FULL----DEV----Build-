"""Reasoning trace writer for the babyg background agent loop.

Every fire of the agent loop for a creator writes one row here,
describing what the pre-filter surfaced, what claude decided, and
which tools ran. The loop never reads back — this is purely for
observability, debugging, and later "why did babyg do X?" surfaces.

Public shape:

    record_cycle(user_id, ...)           -> dict | None
    list_recent(user_id, limit=20)       -> list[dict]
    latest(user_id)                      -> dict | None

The record contract is one call at the end of a cycle (success or
failure). Field defaults are conservative — a partial-info cycle
(e.g. failed before any tool call) still gets a row so we can see
the shape of the failure.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from app.core import supabase_client

logger = logging.getLogger(__name__)

CycleStatus = Literal[
    "ok",
    "skipped_no_delta",
    "skipped_over_cap",
    "skipped_autonomy",
    "failed",
]

_VALID_STATUS: frozenset[str] = frozenset(
    {"ok", "skipped_no_delta", "skipped_over_cap", "skipped_autonomy", "failed"}
)


def prompt_hash(text: str) -> str:
    """Short stable hash of the system prompt used this cycle. Bucketing
    old cycles by prompt version lets us tell 'the agent regressed'
    from 'the prompt changed' when we tune it."""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:12]


def record_cycle(
    user_id: str,
    *,
    status: str,
    cycle_started_at: datetime,
    cycle_ended_at: datetime | None = None,
    delta: dict[str, Any] | None = None,
    tools_called: list[dict[str, Any]] | None = None,
    final_response: str | None = None,
    system_prompt_hash: str | None = None,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    skip_reason: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any] | None:
    """Insert one trace row. Never raises — a failure to record must
    not blow up the loop that just finished.

    Truncates skip_reason/error_message tails so a pathological value
    can't bloat the row.
    """
    if status not in _VALID_STATUS:
        logger.warning("agent_cycles.bad_status status=%s user=%s", status, user_id)
        return None
    started = cycle_started_at.astimezone(UTC).isoformat()
    ended = (cycle_ended_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    row: dict[str, Any] = {
        "user_id": user_id,
        "cycle_started_at": started,
        "cycle_ended_at": ended,
        "status": status,
        "delta": delta or {},
        "tools_called": tools_called or [],
        "final_response": (final_response or None),
        "system_prompt_hash": system_prompt_hash,
        "model": model,
        "prompt_tokens": max(int(prompt_tokens), 0),
        "completion_tokens": max(int(completion_tokens), 0),
        "cost_usd": max(round(float(cost_usd), 6), 0.0),
    }
    if skip_reason:
        row["skip_reason"] = skip_reason[:500]
    if error_class:
        row["error_class"] = error_class[:120]
    if error_message:
        row["error_message"] = error_message[:2000]
    try:
        result = (
            supabase_client.get_service_client()
            .table("agent_cycles")
            .insert(row)
            .execute()
        )
    except Exception:
        logger.exception("agent_cycles.record.write_failed user=%s", user_id)
        return None
    rows = list(getattr(result, "data", None) or [])
    return rows[0] if rows else row


def list_recent(user_id: str, *, limit: int = 20) -> list[dict[str, Any]]:
    capped = max(1, min(int(limit), 200))
    try:
        result = (
            supabase_client.get_service_client()
            .table("agent_cycles")
            .select(
                "id,cycle_started_at,cycle_ended_at,status,skip_reason,"
                "delta,tools_called,final_response,model,"
                "prompt_tokens,completion_tokens,cost_usd,"
                "error_class,error_message"
            )
            .eq("user_id", user_id)
            .order("cycle_started_at", desc=True)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception("agent_cycles.list_recent.read_failed user=%s", user_id)
        return []
    return list(getattr(result, "data", None) or [])


def latest(user_id: str) -> dict[str, Any] | None:
    rows = list_recent(user_id, limit=1)
    return rows[0] if rows else None
