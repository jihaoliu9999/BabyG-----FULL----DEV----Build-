"""Operator-side member roster.

Reads from `users` and joins per-row to creator_profiles or brand_profiles
in Python (volumes are small enough; if it ever profiles hot we move it
to a SQL view).
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


def list_users(
    *, role: str | None = None, page: int = 1, page_size: int = 100
) -> tuple[list[dict[str, Any]], int]:
    """Return (rows, total_count) for the requested page.

    Was a flat list capped at 500 with no signal on truncation. Now uses
    postgrest's `count="exact"` + range so the operator UI can render
    "showing N of M" and a next page link.
    """
    page = max(1, int(page or 1))
    page_size = max(1, min(int(page_size or 100), 500))
    start = (page - 1) * page_size
    end = start + page_size - 1
    try:
        query = (
            supabase_client.get_service_client()
            .table("users")
            .select("*", count="exact")
            .order("created_at", desc=True)
            .range(start, end)
        )
        if role in ("creator", "brand", "operator"):
            query = query.eq("role", role)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("members list_users failed")
        return [], 0
    rows = getattr(result, "data", None) or []
    total = int(getattr(result, "count", 0) or 0)
    return rows, total


def get_user(user_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("users")
            .select("*")
            .eq("id", user_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("members get_user failed: %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def annotate_with_profile(user: dict[str, Any]) -> dict[str, Any]:
    """Look up the role-matching profile and stash it on `user["profile"]`."""
    role = user.get("role")
    user_id = str(user.get("id") or "")
    if role == "creator":
        from app.services import profiles
        user["profile"] = profiles.get_creator_profile(user_id)
    elif role == "brand":
        from app.services import brands
        user["profile"] = brands.get_by_user_id(user_id)
    else:
        user["profile"] = None
    return user
