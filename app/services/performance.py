"""Stored creator performance snapshots."""

from __future__ import annotations

import logging
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
