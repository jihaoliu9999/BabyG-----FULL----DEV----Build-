"""Weekly performance logs — creator self-report (operators can also enter).

Schema (migrations/0002_schema.sql §performance_logs) has UNIQUE on
(user_id, week_start_date), so we use upsert on that pair to allow
re-submission if a creator wants to fix last week's numbers.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


def list_for_user(user_id: str, *, limit: int = 26) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("performance_logs")
            .select("*")
            .eq("user_id", user_id)
            .order("week_start_date", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("performance list failed: %s", user_id)
        return []
    return getattr(result, "data", None) or []


def get_for_week(user_id: str, week_start: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("performance_logs")
            .select("*")
            .eq("user_id", user_id)
            .eq("week_start_date", week_start)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception(
            "performance get_for_week failed: %s / %s", user_id, week_start
        )
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def upsert(
    *, user_id: str, entered_by: str, payload: dict[str, Any]
) -> bool:
    body = {**payload, "user_id": user_id, "entered_by_user_id": entered_by}
    try:
        supabase_client.get_service_client().table("performance_logs").upsert(
            body, on_conflict="user_id,week_start_date"
        ).execute()
    except PostgrestAPIError:
        logger.exception("performance upsert failed: %s", user_id)
        return False
    return True


def last_monday_iso() -> str:
    """Default `week_start_date` for the new-log form: the most recent
    Monday (in UTC, since week_start_date is a DATE)."""
    today = datetime.now(UTC).date()
    return (today - timedelta(days=today.weekday())).isoformat()
