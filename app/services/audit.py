"""Audit log — operator action tracking.

A small write-helper plus a read for the operator audit page. The
existing operator actions (intel publish, brand verify/reject, abuse
resolve, job takedown) call `record` to leave a trail. We don't enforce
this at the schema level; it's a best-effort observability tool, not a
compliance log.
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


def record(
    *,
    actor_user_id: str | None,
    action: str,
    target_type: str | None = None,
    target_id: str | None = None,
    notes: str | None = None,
) -> bool:
    payload: dict[str, Any] = {
        "actor_user_id": actor_user_id,
        "action": action,
        "target_type": target_type,
        "target_id": target_id,
        "notes": notes,
    }
    try:
        supabase_client.get_service_client().table("audit_log").insert(
            payload
        ).execute()
    except PostgrestAPIError:
        # Audit failures must not block the user-facing action.
        logger.exception("audit record failed: action=%s", action)
        return False
    return True


def list_recent(*, limit: int = 200) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("audit_log")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("audit list_recent failed")
        return []
    return getattr(result, "data", None) or []
