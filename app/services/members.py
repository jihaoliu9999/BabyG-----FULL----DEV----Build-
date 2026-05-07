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
    *, role: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    try:
        query = (
            supabase_client.get_service_client()
            .table("users")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if role in ("creator", "brand", "operator"):
            query = query.eq("role", role)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("members list_users failed")
        return []
    return getattr(result, "data", None) or []


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
