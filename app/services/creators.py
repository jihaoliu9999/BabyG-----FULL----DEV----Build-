"""Creator directory — used by brands to discover creators.

Filtering happens in two passes: pg-side filter for completed onboarding,
then a Python pass for niche overlap + size match. Volumes are tiny early
on; we'll move to a SQL function or pg_trgm when this profiles hot.

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


# Maps the creator's onboarding follower band to the size buckets that
# brands pick during their onboarding.
FOLLOWER_BAND_TO_SIZE: dict[str, str] = {
    "<10k": "nano",
    "10-50k": "micro",
    "50-100k": "micro",
    "100-500k": "mid",
    "500k+": "macro",
}


def list_for_brand_match(
    *, niche_preferences: list[str], creator_size_preferences: list[str]
) -> list[dict[str, Any]]:
    """Public-projected list of onboarded creators matching brand prefs."""
    rows = _list_onboarded_creators()

    brand_niches = set(niche_preferences or [])
    brand_sizes = set(creator_size_preferences or [])

    matches: list[dict[str, Any]] = []
    for row in rows:
        creator_niches = set(row.get("niches") or [])
        if brand_niches and not creator_niches.intersection(brand_niches):
            continue

        creator_band = row.get("follower_range")
        creator_size = FOLLOWER_BAND_TO_SIZE.get(creator_band) if creator_band else None
        if brand_sizes and (creator_size is None or creator_size not in brand_sizes):
            continue

        projected = public_creator(row)
        if projected is not None:
            matches.append(projected)

    return matches


def get_for_view(user_id: str) -> dict[str, Any] | None:
    """Cross-user read of a creator profile. Used by brand discovery
    and any other "view someone else's profile" surface. Returns the
    public-field projection — internal fields are intentionally omitted
    so a future template addition can't leak them."""
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
    """Internal: returns the FULL row so `list_for_brand_match` can do
    its niche/size filter on private columns like `follower_range`.
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
