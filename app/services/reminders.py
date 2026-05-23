"""Content reminder writes for creator-owned local reminders."""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


def create(*, user_id: str, payload: dict[str, Any]) -> str | None:
    body = {**payload, "user_id": user_id}
    try:
        result = (
            supabase_client.get_service_client()
            .table("content_reminders")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("content reminder create failed")
        return None
    rows = getattr(result, "data", None) or []
    return str(rows[0]["id"]) if rows else None
