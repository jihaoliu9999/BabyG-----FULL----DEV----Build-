"""Creator directory helpers.

Filtering happens in two passes: pg-side filter for completed onboarding,
then route-level presentation filtering. Volumes are tiny early on; we'll
move to a SQL function or pg_trgm when this profiles hot.

Reads go through `profiles.public_creator` so internal fields
(`baseline_followers`, `tier`, `writing_samples`, etc.) don't leak to
other users. Owner-side reads use `profiles.get_creator_profile`.
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.services.profiles import public_creator

logger = logging.getLogger(__name__)


def get_for_view(user_id: str) -> dict[str, Any] | None:
    """Cross-user read of a creator profile.

    Returns the public-field projection; internal fields are intentionally
    omitted so a future template addition can't leak them.
    """
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_profiles")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("creator get_for_view failed: %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    return public_creator(rows[0]) if rows else None


def _list_onboarded_creators() -> list[dict[str, Any]]:
    """Internal: returns the FULL row for creator-directory routes.

    Callers must `public_creator()` before returning to a route."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_profiles")
            .select("*")
            .not_.is_("onboarding_completed_at", "null")
            .order("onboarding_completed_at", desc=True)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("creator list failed")
        return []
    return getattr(result, "data", None) or []
