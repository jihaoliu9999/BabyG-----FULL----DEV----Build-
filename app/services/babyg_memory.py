"""babyg's own long-lived memory.

Phase 3 of the babyg AI v2 plan. See docs/babyg-ai-reference.md.

Everything babyg remembers about the creator that has to survive across
sessions lives here. Chat history is not enough; a manager who forgets
is not a manager.

Kinds:
    drafts                  Every draft babyg composed. Stays in babyg
                            memory by default. Gmail drafts only saved
                            to Gmail when the creator explicitly asks.
    decisions               Structured record of decisions made.
                            e.g. "passed on Nike gifting", "counter
                            Vans at $2k".
    deals                   Deal state, dollars, deliverables, stage.
                            Linked to touchpoints across surfaces.
    deal_touchpoints        Every DM, email, calendar event, or
                            contract that touched a deal.
    voice_samples           Creator's writing samples for style
                            matching. From sent messages, edit diffs,
                            chip taps.
    contract_flags          Flagged clauses from parsed contract PDFs.
    relationship_notes      What babyg knows about how a brand or
                            person behaves in business terms.
    creator_preferences     Hard preferences: "no nightlife deals",
                            "prefer fri/sat shoots".

Scope rules:
    * Every row is scoped by creator_id (not user_id). One account may
      hold both creator and brand roles once brand side ships.
    * RLS: only the row's creator_id can read.
    * Operator reads route through /operator/trust/{creator_id}/memory,
      which uses the service role and writes a memory_access_audit row
      before returning any data.
    * Retention: never delete. Only the last 12 months preload into
      the system prompt. Older memory is retrievable via explicit tool
      calls with a date range.

All writes go through the service role (RLS enforces the read scope).
Every helper below is best-effort: a Supabase failure never breaks the
turn, it just returns an empty result.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)

MemoryKind = Literal[
    "drafts",
    "decisions",
    "deals",
    "deal_touchpoints",
    "voice_samples",
    "contract_flags",
    "relationship_notes",
    "creator_preferences",
]

# Every kind maps to exactly one Supabase table. Kept as a dict so a
# read at call-site fails loudly on a typo instead of silently going
# to the wrong table.
_KIND_TABLE: dict[str, str] = {
    "drafts": "babyg_memory_drafts",
    "decisions": "babyg_memory_decisions",
    "deals": "babyg_memory_deals",
    "deal_touchpoints": "babyg_memory_deal_touchpoints",
    "voice_samples": "babyg_memory_voice_samples",
    "contract_flags": "babyg_memory_contract_flags",
    "relationship_notes": "babyg_memory_relationship_notes",
    "creator_preferences": "babyg_memory_creator_preferences",
}

# Per-kind date column to filter on for `read(..., since=...)`. Deals
# use last_touch_at, everything else uses created_at.
_KIND_DATE_COLUMN: dict[str, str] = {
    "drafts": "created_at",
    "decisions": "created_at",
    "deals": "last_touch_at",
    "deal_touchpoints": "occurred_at",
    "voice_samples": "created_at",
    "contract_flags": "created_at",
    "relationship_notes": "created_at",
    "creator_preferences": "updated_at",
}

# Preload window into the system prompt. Older rows are still there,
# just retrieved via explicit tool calls.
PRELOAD_WINDOW = timedelta(days=365)


def save(
    kind: MemoryKind,
    creator_id: str,
    payload: dict[str, Any],
) -> dict[str, Any] | None:
    """Insert a memory row for the creator. Returns the inserted row or
    None on failure. Never raises."""
    table = _KIND_TABLE.get(kind)
    if not table:
        logger.info("babyg_memory.save unknown_kind kind=%s", kind)
        return None
    uid = safe_uuid(creator_id)
    if not uid:
        return None
    row = {"creator_id": uid, **payload}
    try:
        result = (
            supabase_client.get_service_client()
            .table(table)
            .insert(row)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        logger.info("babyg_memory.save_failed kind=%s", kind, exc_info=True)
        return None


def read(
    kind: MemoryKind,
    creator_id: str,
    *,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return the creator's memory rows for one kind, newest first.

    `since=None` returns everything up to `limit`. Callers that only
    want the preload window pass `since=default_preload_cutoff()`.
    Never raises; returns [] on failure.
    """
    table = _KIND_TABLE.get(kind)
    date_col = _KIND_DATE_COLUMN.get(kind, "created_at")
    if not table:
        return []
    uid = safe_uuid(creator_id)
    if not uid:
        return []
    limit = max(1, min(int(limit or 100), 1000))
    try:
        query = (
            supabase_client.get_service_client()
            .table(table)
            .select("*")
            .eq("creator_id", uid)
            .order(date_col, desc=True)
            .limit(limit)
        )
        if since is not None:
            query = query.gte(date_col, since.isoformat())
        rows = query.execute()
        return list(rows.data or [])
    except Exception:
        logger.info("babyg_memory.read_failed kind=%s", kind, exc_info=True)
        return []


def default_preload_cutoff() -> datetime:
    """Cutoff timestamp for what preloads into the system prompt.
    Older rows stay in the DB but must be read via an explicit tool
    with a date range."""
    return datetime.now(UTC) - PRELOAD_WINDOW


def read_recent_summary(creator_id: str) -> dict[str, Any]:
    """Compact per-kind counts for the preload window, used by the
    system-prompt injection helper. Not a full read; just:
        {"drafts": 12, "decisions": 3, "deals_active": 2, ...}
    Never raises.
    """
    cutoff = default_preload_cutoff()
    summary: dict[str, Any] = {}
    for kind in _KIND_TABLE:
        try:
            summary[kind] = len(read(kind, creator_id, since=cutoff, limit=1000))
        except Exception:
            summary[kind] = 0
            logger.info("babyg_memory.summary_kind_failed kind=%s", kind, exc_info=True)
    return summary


# ---- Operator access ------------------------------------------------------


def read_for_operator(
    kind: MemoryKind,
    creator_id: str,
    *,
    operator_id: str,
    reason: str,
    since: datetime | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Operator-scoped read. Writes a memory_access_audit row FIRST,
    then returns the memory rows. If the audit write fails, the read
    also refuses. This is the only sanctioned way for an operator to
    see a creator's memory. Called by /operator/trust/{creator_id}/memory.
    """
    op_uid = safe_uuid(operator_id)
    creator_uid = safe_uuid(creator_id)
    reason_clean = (reason or "").strip()
    if not op_uid or not creator_uid:
        logger.info("babyg_memory.operator_read_invalid_ids")
        return []
    if not reason_clean:
        logger.info("babyg_memory.operator_read_missing_reason")
        return []
    if kind not in _KIND_TABLE:
        logger.info("babyg_memory.operator_read_unknown_kind kind=%s", kind)
        return []

    rows = read(kind, creator_id, since=since, limit=limit)
    row_ids = [str(r.get("id")) for r in rows if r.get("id")]

    try:
        supabase_client.get_service_client().table("memory_access_audit").insert({
            "operator_id": op_uid,
            "creator_id": creator_uid,
            "memory_kind": kind,
            "memory_row_ids": row_ids,
            "reason": reason_clean[:1000],
        }).execute()
    except Exception:
        # If we can't record the audit, we don't get to read.
        logger.info("babyg_memory.audit_write_failed kind=%s", kind, exc_info=True)
        return []

    return rows
