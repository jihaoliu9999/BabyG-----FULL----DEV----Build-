"""Per-turn AI observability for babyg.

Phase 1 of the babyg AI v2 plan. See docs/babyg-ai-reference.md.

Every babyg turn (chat, confirm, cancel) calls `TurnRecorder.record(...)`
which inserts a single row into the `bot_turns` table. The row is
metadata only. Never store OAuth tokens, cookies, raw Gmail bodies, raw
contract text, or private credentials here. Tool inputs are hashed, not
stored.

Usage:
    recorder = TurnRecorder.start(user_id="...", model="...", prompt_version="...")
    recorder.note_tool_requested("read_my_profile", input_hash=..., duration_ms=...)
    recorder.note_tool_executed("read_my_profile", ok=True, duration_ms=...)
    recorder.note_guardrail("rate_floor_refusal")
    recorder.note_action_proposal_staged("gmail.send_email")
    recorder.finish(
        response_type="pending_action",
        anthropic_duration_ms=1234,
        input_tokens=..., output_tokens=...,
    )

Failure to write the row must never break the turn itself. Every insert
is wrapped in a broad except that logs and moves on. Observability is
best-effort.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Literal

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)

# The bot_turns insert on each turn costs ~30-80ms of supabase latency
# on the hot path. Move it onto a small worker pool so `finish()` returns
# immediately; the turn returns to the user without waiting on the write.
# Observability is best-effort so a dropped write on shutdown is fine.
_WRITE_POOL_SIZE = 2
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()
_pending: set = set()
_pending_lock = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_WRITE_POOL_SIZE,
                    thread_name_prefix="bot_obs",
                )
                atexit.register(_shutdown_executor)
    return _executor


def _shutdown_executor() -> None:
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=True, cancel_futures=False)
        _executor = None


def _flush_pending_writes(timeout: float | None = 2.0) -> None:
    """Block until all queued bot_turns inserts have completed.

    Not part of the request path — used by tests and process shutdown.
    Returns silently if nothing is pending.
    """
    import contextlib

    with _pending_lock:
        futures = list(_pending)
    for fut in futures:
        with contextlib.suppress(Exception):
            fut.result(timeout=timeout)

ResponseType = Literal["text", "refusal", "pending_action", "error"]
Role = Literal["creator", "brand", "operator"]

_GUARDRAIL_NAMES = {
    "rate_floor_refusal",
    "override_floor_used",
    "scope_refusal",
    "payment_keyword_block",
    "tool_iteration_cap_hit",
    "user_message_truncated",
}


@dataclass
class TurnRecorder:
    """Buffers turn metadata as the turn runs, writes one row on finish.

    Instances are cheap. Do not share across turns. `start(...)` is a
    classmethod so callers do not have to construct fields manually.
    """

    user_id: str
    model: str
    prompt_version: str
    role: Role = "creator"
    conversation_id: str | None = None
    thread_id: str | None = None
    feature_flags_snapshot: dict[str, Any] = field(default_factory=dict)

    _started_at_monotonic: float = field(default_factory=time.monotonic)
    _tools_requested: list[dict[str, Any]] = field(default_factory=list)
    _tools_executed: list[dict[str, Any]] = field(default_factory=list)
    _tool_errors: list[dict[str, Any]] = field(default_factory=list)
    _action_proposals_staged: list[str] = field(default_factory=list)
    _guardrails_triggered: list[str] = field(default_factory=list)
    _finished: bool = False

    @classmethod
    def start(
        cls,
        *,
        user_id: str,
        model: str,
        prompt_version: str,
        role: Role = "creator",
        conversation_id: str | None = None,
        thread_id: str | None = None,
        feature_flags_snapshot: dict[str, Any] | None = None,
    ) -> TurnRecorder:
        return cls(
            user_id=str(user_id),
            model=str(model),
            prompt_version=str(prompt_version),
            role=role,
            conversation_id=conversation_id,
            thread_id=thread_id,
            feature_flags_snapshot=dict(feature_flags_snapshot or {}),
        )

    # ---- accumulators (safe to call as many times as needed) --------------

    def note_tool_requested(
        self, name: str, *, input_hash: str | None = None, duration_ms: int | None = None
    ) -> None:
        entry: dict[str, Any] = {"name": name}
        if input_hash is not None:
            entry["input_hash"] = input_hash
        if duration_ms is not None:
            entry["duration_ms"] = int(duration_ms)
        self._tools_requested.append(entry)

    def note_tool_executed(
        self, name: str, *, ok: bool, duration_ms: int | None = None
    ) -> None:
        entry: dict[str, Any] = {"name": name, "ok": bool(ok)}
        if duration_ms is not None:
            entry["duration_ms"] = int(duration_ms)
        self._tools_executed.append(entry)

    def note_tool_error(self, name: str, error: str) -> None:
        self._tool_errors.append({"name": name, "error": str(error)[:400]})

    def note_action_proposal_staged(self, action_type: str) -> None:
        self._action_proposals_staged.append(str(action_type))

    def note_guardrail(self, name: str) -> None:
        if name not in _GUARDRAIL_NAMES:
            logger.info("bot_observability.unknown_guardrail", extra={"name": name})
        self._guardrails_triggered.append(str(name))

    # ---- finalize ---------------------------------------------------------

    def finish(
        self,
        *,
        response_type: ResponseType,
        anthropic_duration_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        error_message: str | None = None,
    ) -> None:
        """Insert the row. Never raises; observability failures do not
        break the turn. Safe to call once; second call is a no-op."""
        if self._finished:
            return
        self._finished = True

        total_duration_ms = int((time.monotonic() - self._started_at_monotonic) * 1000)

        uid = safe_uuid(self.user_id)
        if not uid:
            logger.info("bot_observability.invalid_user_id skip")
            return

        payload = {
            "user_id": uid,
            "role": self.role,
            "conversation_id": safe_uuid(self.conversation_id) if self.conversation_id else None,
            "thread_id": safe_uuid(self.thread_id) if self.thread_id else None,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "finished_at": _now_iso(),
            "total_duration_ms": total_duration_ms,
            "anthropic_duration_ms": anthropic_duration_ms,
            "tools_requested": self._tools_requested,
            "tools_executed": self._tools_executed,
            "tool_errors": self._tool_errors,
            "action_proposals_staged": self._action_proposals_staged,
            "guardrails_triggered": self._guardrails_triggered,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "response_type": response_type,
            "error_message": (error_message or None) if error_message else None,
            "feature_flags_snapshot": self.feature_flags_snapshot,
        }
        _enqueue_insert(payload)


# ---- helpers --------------------------------------------------------------


def hash_tool_input(payload: Any) -> str:
    """Short opaque hash of a tool_input dict, safe for logging.

    Used so we can see 'this tool was called with the same input twice'
    without ever storing the payload itself. Any nested dicts are
    JSON-serialized with sorted keys for a stable hash.
    """
    try:
        raw = json.dumps(payload, sort_keys=True, default=str)
    except Exception:
        raw = str(payload)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()


def _safe_insert(payload: dict[str, Any]) -> None:
    try:
        supabase_client.get_service_client().table("bot_turns").insert(payload).execute()
    except Exception:
        # Observability is best-effort. Never break the turn on a write
        # failure. Log at info so it lands in Railway logs but does not
        # look like a real error.
        logger.info("bot_observability.write_failed", exc_info=True)


def _enqueue_insert(payload: dict[str, Any]) -> None:
    """Submit the insert to the shared executor. Fire-and-forget: a
    submit failure or a dead executor falls back to the synchronous
    write so a shutting-down process still records what it can."""
    try:
        executor = _get_executor()
        future = executor.submit(_safe_insert, payload)
    except RuntimeError:
        _safe_insert(payload)
        return
    except Exception:
        logger.info("bot_observability.enqueue_failed", exc_info=True)
        _safe_insert(payload)
        return
    with _pending_lock:
        _pending.add(future)
    future.add_done_callback(_drop_pending)


def _drop_pending(future) -> None:
    with _pending_lock:
        _pending.discard(future)


def spend_this_month(creator_id: str) -> dict[str, Any]:
    """Return {input_tokens, output_tokens, turn_count} for the current
    calendar month for one creator. Used by the cost-cap surface. Never
    raises; on failure returns zeros so the UI degrades gracefully.
    """
    uid = safe_uuid(creator_id)
    zero = {"input_tokens": 0, "output_tokens": 0, "turn_count": 0}
    if not uid:
        return zero
    try:
        from datetime import UTC, datetime

        month_start = datetime.now(UTC).replace(
            day=1, hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        rows = (
            supabase_client.get_service_client()
            .table("bot_turns")
            .select("input_tokens,output_tokens")
            .eq("user_id", uid)
            .gte("started_at", month_start)
            .limit(10000)
            .execute()
        )
        data = rows.data or []
        return {
            "input_tokens": sum(int(r.get("input_tokens") or 0) for r in data),
            "output_tokens": sum(int(r.get("output_tokens") or 0) for r in data),
            "turn_count": len(data),
        }
    except Exception:
        logger.info("bot_observability.spend_query_failed", exc_info=True)
        return zero
