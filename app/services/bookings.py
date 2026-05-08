"""Bookings — in-app calendar entries for creators.

Schema (migrations/0002_schema.sql §bookings) supports Google Calendar
synchronization via google_calendar_id + google_event_id, but those stay
NULL in this step. The bot's Google Calendar tool-use ships in Phase 2
and will populate the sync columns on the same rows.

Status values: pending | confirmed | cancelled.
Type values:   restaurant | event | collab | brand | reminder.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


TYPES = ("restaurant", "event", "collab", "brand", "reminder")
STATUSES = ("pending", "confirmed", "cancelled")


def list_for_user(
    user_id: str,
    *,
    horizon: str = "all",        # "upcoming" | "past" | "all"
    limit: int = 200,
) -> list[dict[str, Any]]:
    # "upcoming" reads naturally chronologically (next event first); "past"
    # and "all" want most-recent-first instead — past chronological order
    # buries today's actionable items under last year's archive.
    desc = horizon != "upcoming"
    try:
        query = (
            supabase_client.get_service_client()
            .table("bookings")
            .select("*")
            .eq("user_id", user_id)
            .neq("status", "cancelled")
            .order("starts_at", desc=desc)
            .limit(limit)
        )
        if horizon == "upcoming":
            query = query.gte("starts_at", _now_iso())
        elif horizon == "past":
            query = query.lt("starts_at", _now_iso())
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("bookings list failed: %s", user_id)
        return []
    return getattr(result, "data", None) or []


def get(booking_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("bookings")
            .select("*")
            .eq("id", booking_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("bookings get failed: %s", booking_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def create(*, user_id: str, payload: dict[str, Any]) -> str | None:
    body = {**payload, "user_id": user_id}
    try:
        result = (
            supabase_client.get_service_client()
            .table("bookings")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("bookings create failed")
        return None
    rows = getattr(result, "data", None) or []
    return str(rows[0]["id"]) if rows else None


def update(booking_id: str, *, user_id: str, payload: dict[str, Any]) -> bool:
    """Owner-only update — the eq on user_id stops cross-user writes even if
    the route layer ever slipped. Returns True if a row updated."""
    if not payload:
        return True
    try:
        result = (
            supabase_client.get_service_client()
            .table("bookings")
            .update(payload)
            .eq("id", booking_id)
            .eq("user_id", user_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("bookings update failed: %s", booking_id)
        return False
    return bool(getattr(result, "data", None))


def cancel(booking_id: str, *, user_id: str) -> bool:
    return update(booking_id, user_id=user_id, payload={"status": "cancelled"})


def upcoming_count(user_id: str) -> int:
    """Count of upcoming, non-cancelled bookings — used for the dashboard
    quicklink badge."""
    return len(list_for_user(user_id, horizon="upcoming", limit=50))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
