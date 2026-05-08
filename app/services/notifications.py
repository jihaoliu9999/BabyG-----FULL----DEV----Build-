"""Notifications fan-out.

Used by every product surface that needs to ping a user: brand
verification result, brand-to-creator outreach, flagged-message updates,
booking reminders, etc.

The `notifications.kind` column has a CHECK constraint in the schema —
keep KINDS in sync with migrations/0002_schema.sql §notifications.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

# Schema is `body text` (unbounded); cap at insert time so a pathological
# caller can't insert a multi-MB body and inflate the table.
_BODY_MAX = 2000
_TITLE_MAX = 240

logger = logging.getLogger(__name__)


KINDS = [
    "intel_push",
    "booking_reminder",
    "flag_update",
    "collab_match",
    "connection_request",
    "profile_view_digest",
    "job_match",
    "new_dm",
    "system",
]


def create(
    *,
    user_id: str,
    kind: str,
    title: str,
    body: str | None = None,
    link_path: str | None = None,
) -> bool:
    if kind not in KINDS:
        logger.error("notifications.create rejected unknown kind: %s", kind)
        return False
    payload: dict[str, Any] = {
        "user_id": user_id,
        "kind": kind,
        "title": (title or "")[:_TITLE_MAX],
        "body": body[:_BODY_MAX] if isinstance(body, str) else body,
        "link_path": link_path,
    }
    try:
        supabase_client.get_service_client().table("notifications").insert(
            payload
        ).execute()
    except PostgrestAPIError:
        logger.exception("notifications.create failed for %s", user_id)
        return False
    return True


def list_for_user(user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("notifications.list_for_user failed: %s", user_id)
        return []
    return getattr(result, "data", None) or []


def unread_count(user_id: str) -> int:
    try:
        result = (
            supabase_client.get_service_client()
            .table("notifications")
            .select("id", count="exact")
            .eq("user_id", user_id)
            .eq("is_read", False)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("notifications.unread_count failed: %s", user_id)
        return 0
    return int(getattr(result, "count", 0) or 0)


def list_unread(user_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("notifications")
            .select("*")
            .eq("user_id", user_id)
            .eq("is_read", False)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("notifications.list_unread failed: %s", user_id)
        return []
    return getattr(result, "data", None) or []


def mark_read(*, user_id: str, notification_id: str) -> bool:
    """Mark one notification as read. Returns True if a row was updated."""
    payload = {"is_read": True, "read_at": datetime.now(UTC).isoformat()}
    try:
        result = (
            supabase_client.get_service_client()
            .table("notifications")
            .update(payload)
            .eq("user_id", user_id)
            .eq("id", notification_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception(
            "notifications.mark_read failed: user=%s id=%s", user_id, notification_id
        )
        return False
    return bool(getattr(result, "data", None))


def mark_all_read(user_id: str) -> int:
    """Mark every unread notification for `user_id` as read.

    Returns the number of rows updated (0 if there was nothing to mark)
    so callers can tell a real action apart from a no-op. Was previously
    `bool` and always returned True, including on noop.
    """
    payload = {"is_read": True, "read_at": datetime.now(UTC).isoformat()}
    try:
        result = (
            supabase_client.get_service_client()
            .table("notifications")
            .update(payload)
            .eq("user_id", user_id)
            .eq("is_read", False)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("notifications.mark_all_read failed: %s", user_id)
        return 0
    return len(getattr(result, "data", None) or [])
