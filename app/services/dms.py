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

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)


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
    participant_id: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Most-recent `limit` messages, oldest-first for the template.

    Ordering DESC at the DB so we keep the latest tail when the limit
    bites; reversing in Python gives the template the chronological order
    it renders. If `participant_id` is passed we assert the caller is a
    participant of the thread before returning anything — defense in
    depth against any future caller that bypasses route-level checks.
    """
    if participant_id is not None and not _is_participant(thread_id, participant_id):
        return []
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
    """Insert a message and bump the thread's last_message_at."""
    body = (body or "").strip()
    if not body:
        return None
    if len(body) > 4000:
        body = body[:4000]

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
            .select("id", count="exact", head=True)
            .in_("thread_id", thread_ids)
            .neq("sender_id", uid)
            .is_("read_at", "null")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("unread_count_for_user failed: %s", user_id)
        return 0
    return int(getattr(result, "count", 0) or 0)


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
