"""Direct messages — threads + messages + read state.

Schema (migrations/0002_schema.sql):
  dm_threads(participant_a_id, participant_b_id) with a UNIQUE pair and a
  CHECK that a < b. We canonicalize the pair on every read/write so a
  thread between (X, Y) and (Y, X) is the same row.

  dm_messages(thread_id, sender_id, body, read_at, created_at).

Uses the service-role client. Routes always auth-gate first AND verify
that the caller is one of the two participants on every read/write.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError
from postgrest.types import CountMethod

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Access gates
# -----------------------------------------------------------------------------


def recipient_accepts_cold_thread(recipient_profile: dict[str, Any] | None) -> bool:
    """Does the recipient allow an unconnected opener to start a thread?

    Reads ``dm_preference`` from the recipient's profile (migration 0016):

      * ``open``              — yes, anyone may open. Default for creators
                                who haven't picked a preference.
      * ``connections_only``  — no, opener must already be an accepted
                                connection (or, when brand→creator DMs
                                land in Phase 2, an opportunity-matched
                                party).

    Today's creator-to-creator route enforces an accepted-connection
    requirement unconditionally, so this helper has no live consumer in
    v1. It's the contract Phase 2 brand→creator DMs will call into:
    when a brand tries to cold-DM a creator with
    ``dm_preference=connections_only``, the call should refuse. Locking
    the shape down here means the Phase 2 work doesn't have to invent
    the gate from scratch.
    """
    if not recipient_profile:
        # No profile means we can't honor the preference — fail closed.
        return False
    pref = (recipient_profile.get("dm_preference") or "open").strip().lower()
    return pref != "connections_only"


# -----------------------------------------------------------------------------
# Threads
# -----------------------------------------------------------------------------


def get_or_create_thread(user_a: str, user_b: str) -> dict[str, Any] | None:
    """Idempotent: returns the (a,b) thread, creating it if absent."""
    if user_a == user_b:
        return None
    a, b = _canonical_pair(user_a, user_b)

    existing = _find_thread(a, b)
    if existing is not None:
        return existing

    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_threads")
            .upsert(
                {"participant_a_id": a, "participant_b_id": b},
                on_conflict="participant_a_id,participant_b_id",
            )
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("dm_threads upsert failed for (%s, %s)", a, b)
        return None
    rows = getattr(result, "data", None) or []
    if rows:
        return rows[0]
    # Upsert returned nothing — re-fetch (e.g. RLS truncating returning).
    return _find_thread(a, b)


def get_thread_between(user_a: str, user_b: str) -> dict[str, Any] | None:
    a, b = _canonical_pair(user_a, user_b)
    return _find_thread(a, b)


def list_threads_for_user(user_id: str) -> list[dict[str, Any]]:
    """Return threads the user participates in, newest activity first.

    Each row carries `peer_id` (the other participant) so the caller doesn't
    have to remember which side of the pair the user is on.
    """
    uid = safe_uuid(user_id)
    if not uid:
        return []
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_threads")
            .select("*")
            .or_(f"participant_a_id.eq.{uid},participant_b_id.eq.{uid}")
            .order("last_message_at", desc=True, nullsfirst=False)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("dm threads list failed for %s", user_id)
        return []
    rows = getattr(result, "data", None) or []
    for r in rows:
        r["peer_id"] = (
            r["participant_b_id"]
            if r["participant_a_id"] == user_id
            else r["participant_a_id"]
        )
    return rows


# -----------------------------------------------------------------------------
# Messages
# -----------------------------------------------------------------------------


def list_messages(
    thread_id: str,
    *,
    participant_id: str,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Most-recent `limit` messages, oldest-first for the template.

    `participant_id` is REQUIRED: every caller must prove they're a
    participant before we return any row. Operators that legitimately
    need to read any thread (abuse review) use the explicit
    `list_messages_for_operator` helper.

    Ordering DESC at the DB so we keep the latest tail when the limit
    bites; reversing in Python gives the template the chronological order
    it renders.
    """
    if not _is_participant(thread_id, participant_id):
        return []
    return _read_messages(thread_id, limit=limit)


def list_messages_for_operator(
    thread_id: str, *, limit: int = 200
) -> list[dict[str, Any]]:
    """Privileged read used by the abuse review surface. Caller is
    responsible for enforcing the operator role; no participant check.
    """
    return _read_messages(thread_id, limit=limit)


def _read_messages(thread_id: str, *, limit: int) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_messages")
            .select("*")
            .eq("thread_id", thread_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("dm messages list failed for thread %s", thread_id)
        return []
    rows = getattr(result, "data", None) or []
    rows.reverse()
    return rows


def _is_participant(thread_id: str, user_id: str) -> bool:
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_threads")
            .select("participant_a_id, participant_b_id")
            .eq("id", thread_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("participant check failed for thread %s", thread_id)
        return False
    rows = getattr(result, "data", None) or []
    if not rows:
        return False
    row = rows[0]
    return user_id in (row.get("participant_a_id"), row.get("participant_b_id"))


def send_message(
    *, thread_id: str, sender_id: str, body: str
) -> dict[str, Any] | None:
    """Insert a message and bump the thread's last_message_at.

    Refuses to write if `sender_id` is not a participant of
    `thread_id` — defense in depth against a future caller that
    forwards a URL-supplied thread_id raw.
    """
    body = (body or "").strip()
    if not body:
        return None
    if len(body) > 4000:
        body = body[:4000]
    if not _is_participant(thread_id, sender_id):
        logger.warning(
            "send_message refused: sender=%s not a participant of thread=%s",
            sender_id,
            thread_id,
        )
        return None

    now = _now_iso()
    try:
        msg_result = (
            supabase_client.get_service_client()
            .table("dm_messages")
            .insert(
                {
                    "thread_id": thread_id,
                    "sender_id": sender_id,
                    "body": body,
                }
            )
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("dm send failed: thread=%s sender=%s", thread_id, sender_id)
        return None
    rows = getattr(msg_result, "data", None) or []
    if not rows:
        return None
    msg = rows[0]

    # Best-effort bump. If this fails the thread will still surface in the
    # list because we order by last_message_at desc nullsfirst=False; the
    # next successful send will fix the timestamp.
    try:
        supabase_client.get_service_client().table("dm_threads").update(
            {"last_message_at": now}
        ).eq("id", thread_id).execute()
    except PostgrestAPIError:
        logger.exception("dm thread bump failed: %s", thread_id)
    return msg


def mark_thread_read_for(thread_id: str, *, reader_id: str) -> int:
    """Mark every unread message in `thread_id` not sent by `reader_id`
    as read. Returns the count updated (best-effort)."""
    now = _now_iso()
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_messages")
            .update({"read_at": now})
            .eq("thread_id", thread_id)
            .neq("sender_id", reader_id)
            .is_("read_at", "null")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("mark_thread_read failed: thread=%s reader=%s", thread_id, reader_id)
        return 0
    rows = getattr(result, "data", None) or []
    return len(rows)


def unread_count_for_user(user_id: str) -> int:
    """Total unread messages addressed to this user across all threads.

    Two postgrest round-trips: list the user's thread ids (rows only,
    no message payload), then `count="exact", head=True` against
    dm_messages filtered by those ids + sender != me + read_at is null.
    Postgrest can't express "exists" cheaply enough to fold this into
    one query; the second call returns just the count header so volume
    cost is minimal. A future swap to a Postgres view or RPC could
    collapse this to one trip.
    """
    uid = safe_uuid(user_id)
    if not uid:
        return 0
    try:
        threads_result = (
            supabase_client.get_service_client()
            .table("dm_threads")
            .select("id")
            .or_(f"participant_a_id.eq.{uid},participant_b_id.eq.{uid}")
            .execute()
        )
        thread_ids = [
            str(r["id"]) for r in (getattr(threads_result, "data", None) or [])
        ]
        if not thread_ids:
            return 0
        result = (
            supabase_client.get_service_client()
            .table("dm_messages")
            .select("id", count=CountMethod.exact, head=True)
            .in_("thread_id", thread_ids)
            .neq("sender_id", uid)
            .is_("read_at", "null")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("unread_count_for_user failed: %s", user_id)
        return 0
    return int(getattr(result, "count", 0) or 0)


def unread_counts_by_thread(
    user_id: str, thread_ids: list[str]
) -> dict[str, int]:
    """Return {thread_id: count of unread messages for user} in one query.

    The DM list renders a row per thread; needing an unread count per row
    without an N+1 query. Postgrest can't natively do "group by thread_id
    with count filter" over multiple ids, so we fetch just the unread
    message ids + thread_id (no bodies) and bucket in Python. Bounded
    by the number of unread messages, which is small for any real user.
    """
    uid = safe_uuid(user_id)
    if not uid or not thread_ids:
        return {}
    safe_ids = [tid for tid in (safe_uuid(t) for t in thread_ids) if tid]
    if not safe_ids:
        return {}
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_messages")
            .select("thread_id")
            .in_("thread_id", safe_ids)
            .neq("sender_id", uid)
            .is_("read_at", "null")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("unread_counts_by_thread failed: %s", user_id)
        return {}
    counts: dict[str, int] = {}
    for row in getattr(result, "data", None) or []:
        tid = str(row.get("thread_id") or "")
        if tid:
            counts[tid] = counts.get(tid, 0) + 1
    return counts


def last_messages_by_thread(thread_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Return {thread_id: {body, sender_id, created_at}} for the newest
    message of each thread — one query.

    The inbox row wants a preview line under each name. Rather than an
    N+1 (one query per thread), we fetch a bounded slice of the newest
    messages across all requested threads, sorted DESC, and keep the
    first one seen per thread. Bounded by ``limit`` — safe for the ~20
    threads a real user has open at once.
    """
    if not thread_ids:
        return {}
    safe_ids = [tid for tid in (safe_uuid(t) for t in thread_ids) if tid]
    if not safe_ids:
        return {}
    limit = max(len(safe_ids) * 6, 60)
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_messages")
            .select("thread_id, body, sender_id, created_at")
            .in_("thread_id", safe_ids)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("last_messages_by_thread failed")
        return {}
    latest: dict[str, dict[str, Any]] = {}
    for row in getattr(result, "data", None) or []:
        tid = str(row.get("thread_id") or "")
        if tid and tid not in latest:
            latest[tid] = {
                "body": row.get("body") or "",
                "sender_id": str(row.get("sender_id") or ""),
                "created_at": row.get("created_at"),
            }
    return latest


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _canonical_pair(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a < b else (b, a)


def _find_thread(a: str, b: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("dm_threads")
            .select("*")
            .eq("participant_a_id", a)
            .eq("participant_b_id", b)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("dm thread lookup failed for (%s, %s)", a, b)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
