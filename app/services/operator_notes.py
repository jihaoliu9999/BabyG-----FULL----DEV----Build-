"""Operator-private notes attached to a user."""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


def list_for_user(target_user_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("operator_notes")
            .select("*")
            .eq("target_user_id", target_user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("operator_notes list failed: %s", target_user_id)
        return []
    return getattr(result, "data", None) or []


def create(
    *, target_user_id: str, author_user_id: str, body: str
) -> bool:
    body = (body or "").strip()
    if not body:
        return False
    try:
        supabase_client.get_service_client().table("operator_notes").insert(
            {
                "target_user_id": target_user_id,
                "author_user_id": author_user_id,
                "body": body[:2000],
            }
        ).execute()
    except PostgrestAPIError:
        logger.exception("operator_notes create failed")
        return False
    return True
