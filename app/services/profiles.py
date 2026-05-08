"""Profile data access for creator and brand onboarding.

Uses the service-role client so it bypasses RLS. Safe here because every
caller is server-side and gated by `require_role` in the route layer — by
the time we hit this module, the user_id has already been read from the
signed session cookie and the role has been checked. Never expose any of
these functions over a public endpoint without an auth check first.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Read
# -----------------------------------------------------------------------------


def get_creator_profile(user_id: str) -> dict[str, Any] | None:
    return _get_profile("creator_profiles", user_id)


def get_brand_profile(user_id: str) -> dict[str, Any] | None:
    return _get_profile("brand_profiles", user_id)


def is_creator_onboarded(user_id: str) -> bool:
    profile = get_creator_profile(user_id)
    return bool(profile and profile.get("onboarding_completed_at"))


def is_brand_onboarded(user_id: str) -> bool:
    profile = get_brand_profile(user_id)
    return bool(profile and profile.get("onboarding_completed_at"))


def _get_profile(table: str, user_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table(table)
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("failed to load %s for %s", table, user_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


# -----------------------------------------------------------------------------
# Write
# -----------------------------------------------------------------------------


def update_creator_profile(user_id: str, payload: dict[str, Any]) -> bool:
    return _update_profile("creator_profiles", user_id, payload)


def update_brand_profile(user_id: str, payload: dict[str, Any]) -> bool:
    return _update_profile("brand_profiles", user_id, payload)


class HandleAlreadyTakenError(RuntimeError):
    """Raised when an instagram_handle update would violate the UNIQUE
    constraint on creator_profiles.instagram_handle. Routes catch this and
    surface a specific error message instead of a generic "couldn't save"."""


def complete_creator_onboarding(user_id: str, payload: dict[str, Any]) -> bool:
    return _update_profile(
        "creator_profiles",
        user_id,
        {**payload, "onboarding_completed_at": _now_iso()},
    )


def complete_brand_onboarding(user_id: str, payload: dict[str, Any]) -> bool:
    return _update_profile(
        "brand_profiles",
        user_id,
        {**payload, "onboarding_completed_at": _now_iso()},
    )


def _update_profile(table: str, user_id: str, payload: dict[str, Any]) -> bool:
    if not payload:
        return True
    try:
        result = (
            supabase_client.get_service_client()
            .table(table)
            .update(payload)
            .eq("user_id", user_id)
            .execute()
        )
    except PostgrestAPIError as e:
        # 23505 = Postgres unique_violation. The only UNIQUE on a writable
        # field today is creator_profiles.instagram_handle, so we surface a
        # specific error so the route can render a useful message.
        if (
            getattr(e, "code", "") == "23505"
            and table == "creator_profiles"
            and "instagram_handle" in payload
        ):
            raise HandleAlreadyTakenError from e
        logger.exception("failed to update %s for %s", table, user_id)
        return False
    return bool(getattr(result, "data", None))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
