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
import re
from collections import OrderedDict
from datetime import UTC, date, datetime
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


# Maximum number of day groups the /creator/profile/settings "recent
# changes" panel will surface. History rows past this cap remain in
# ``creator_agent_memory_history`` and are still returned by
# ``history()``; only the summarized presentation is bounded.
RECENT_CHANGES_MAX_GROUPS = 2

# Internal identifiers we never want to render into a user-facing
# summary line: uuids, cycle ids, thread hashes, etc. These sneak into
# ``change_reason`` occasionally when the autonomous loop justifies a
# rewrite by naming the row it just read. Kept lightweight and
# deterministic — no external calls, no LLM. The three patterns each
# target a shape rather than a specific column, so a new source of
# noise (e.g. a fresh idempotency key format) still gets stripped
# without a code change.
_ID_PATTERNS: tuple[re.Pattern[str], ...] = (
    # v4 uuid
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I),
    # opaque long hex/base32 ids (thread hashes, message ids, …)
    re.compile(r"\b[A-Fa-f0-9]{16,}\b"),
    # prefixed ids like "cycle_", "thread_", "msg_", "prop_", "gmail_"…
    re.compile(r"\b(?:cycle|thread|msg|prop|gmail|ig|dm|action|nudge)_[A-Za-z0-9_-]+\b"),
)


def _strip_internal_ids(text: str) -> str:
    """Drop anything that looks like an internal identifier from a
    memory-history change_reason before we render it in Settings."""
    cleaned = text
    for pattern in _ID_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    # Collapse whitespace and stray leftover punctuation from the
    # substitutions above.
    cleaned = re.sub(r"\s{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:!?])", r"\1", cleaned)
    cleaned = cleaned.strip(" ,.;:")
    return cleaned


def _parse_created_at(value: Any) -> datetime | None:
    """Best-effort ISO 8601 parse. Returns None on any failure so a
    single bad row never blanks the whole summary."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _day_label(bucket: date, today: date) -> str:
    delta = (today - bucket).days
    if delta <= 0:
        return "today"
    if delta == 1:
        return "yesterday"
    return bucket.strftime("%b %-d").lower()


def _humanize_reasons(reasons: list[str]) -> str:
    """Combine per-row change_reason strings into one compact sentence.

    Deterministic: no LLM, no external call, no fabricated events. If
    every reason for the day is missing or reduces to noise, falls
    back to a neutral "babyg updated your memory" line so the group
    still renders something legible.
    """
    seen: set[str] = set()
    cleaned: list[str] = []
    for raw in reasons:
        if not isinstance(raw, str):
            continue
        clean = _strip_internal_ids(raw)
        if not clean:
            continue
        # Drop repeats (agent often rewrites for the same delta cause
        # multiple times a day).
        key = clean.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(clean)
    if not cleaned:
        return "babyg updated your memory."
    joined = "; ".join(cleaned[:3])
    if not joined.endswith("."):
        joined += "."
    # Cap the compact line so a chatty day never overflows the panel.
    return joined if len(joined) <= 240 else joined[:237].rstrip() + "…"


def summarize_recent_changes(
    rows: list[dict[str, Any]] | None,
    *,
    now: datetime | None = None,
    max_groups: int = RECENT_CHANGES_MAX_GROUPS,
) -> list[dict[str, Any]]:
    """Collapse raw history rows into at most ``max_groups`` day-labeled
    summary items for the Settings panel.

    ``rows`` are the same shape ``history()`` returns and are treated
    as read-only. Nothing is deleted, nothing is written back. The
    return shape is:

        [{"label": "today", "day": date, "summary": "…"}, …]

    Ordering is most-recent-day first. When the raw history skips
    calendar days (e.g. a run of memory changes on Tuesday and nothing
    on Wednesday), we still return the two most recent days that
    actually have data — the label reflects the real calendar date so
    the panel never invents a "yesterday" for a day the agent stayed
    silent.
    """
    if not rows:
        return []
    max_groups = max(1, int(max_groups))
    today = (now or datetime.now(UTC)).astimezone(UTC).date()
    # OrderedDict preserves first-seen order which, given the rows are
    # already newest-first from history(), is the most-recent-day
    # ordering we want to render.
    grouped: OrderedDict[date, dict[str, Any]] = OrderedDict()
    for row in rows:
        if not isinstance(row, dict):
            continue
        created_at = _parse_created_at(row.get("created_at"))
        if created_at is None:
            continue
        bucket = created_at.date()
        entry = grouped.get(bucket)
        if entry is None:
            entry = {"day": bucket, "reasons": [], "actors": set()}
            grouped[bucket] = entry
            if len(grouped) > max_groups:
                # Stop reading further rows once we already have enough
                # day groups — the raw list can be long and we've
                # captured everything we'll render.
                grouped.pop(bucket)
                break
        reason = row.get("change_reason")
        if isinstance(reason, str) and reason.strip():
            entry["reasons"].append(reason)
        updated_by = row.get("updated_by")
        if isinstance(updated_by, str) and updated_by.strip():
            entry["actors"].add(updated_by.strip().lower())
    out: list[dict[str, Any]] = []
    for bucket, entry in grouped.items():
        summary = _humanize_reasons(entry["reasons"])
        # If only the user edited that day, phrase it that way so
        # "babyg updated" isn't a lie.
        if not entry["reasons"] and entry["actors"] == {"user"}:
            summary = "you edited your memory."
        elif not entry["reasons"] and entry["actors"] == {"agent"}:
            summary = "babyg updated your memory."
        out.append({
            "label": _day_label(bucket, today),
            "day": bucket,
            "summary": summary,
        })
    return out
