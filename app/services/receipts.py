"""Content receipts — what the creator actually shipped.

Schema (migrations/0002_schema.sql §content_receipts). Receipts can
optionally link back to an `intel_post` so we can later count "intel that
turned into content" — those joins arrive with the bot/analytics layer
in Phase 2.
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


POST_TYPES = ("reel", "carousel", "static", "story", "long_form")


def list_for_user(user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("content_receipts")
            .select("*")
            .eq("user_id", user_id)
            .order("posted_at", desc=True, nullsfirst=False)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("receipts list failed: %s", user_id)
        return []
    return getattr(result, "data", None) or []


def create(*, user_id: str, payload: dict[str, Any]) -> str | None:
    body = {**payload, "user_id": user_id}
    try:
        result = (
            supabase_client.get_service_client()
            .table("content_receipts")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("receipts create failed")
        return None
    rows = getattr(result, "data", None) or []
    return str(rows[0]["id"]) if rows else None
