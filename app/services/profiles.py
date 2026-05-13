"""Profile data access for creator onboarding (v1 creator-only scope).

Uses the service-role client so it bypasses RLS. Safe here because every
caller is server-side and gated by `require_role` in the route layer — by
the time we hit this module, the user_id has already been read from the
signed session cookie and the role has been checked. Never expose any of
these functions over a public endpoint without an auth check first.

**Public vs. private fields.** Owner-side reads (a creator looking at
their own dashboard) get the full row. Cross-user reads (peers viewing
each other, operator-side surfaces, future agent tools) MUST go through
`public_creator` so internal fields (`baseline_followers`, `tier`,
`writing_samples`, etc.) don't leak. Service helpers in `creators.py`
and `network.py` apply this projection.

Brand-side profile helpers (`get_brand_profile`, `update_brand_profile`,
`complete_brand_onboarding`, `public_brand`, `PUBLIC_BRAND_FIELDS`)
shipped in v1 but were removed when brand scope deferred to v1.5.
See the brand-side-v1.5 branch for the full set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


# Fields visible to other users (brands viewing creators, creators
# viewing connected peers, operators viewing anyone). Internal fields
# (`baseline_followers`, `tier`, `writing_samples`,
# `notification_settings`, `sub_bot_persona`) are deliberately omitted.
PUBLIC_CREATOR_FIELDS: tuple[str, ...] = (
    "user_id",
    "full_name",
    "instagram_handle",
    "primary_platform",
    "neighborhood",
    "niches",
    "content_formats",
    "follower_range",
    "engagement_range",
    "creator_tenure",
    "bio",
    "hard_limits",
    "onboarding_completed_at",
)

def public_creator(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project a creator row to fields safe to render to non-owners."""
    if row is None:
        return None
    return {k: row.get(k) for k in PUBLIC_CREATOR_FIELDS}


# -----------------------------------------------------------------------------
# Read
# -----------------------------------------------------------------------------


def get_creator_profile(user_id: str) -> dict[str, Any] | None:
    """Owner-side full read. Caller is expected to be the profile owner
    (or an operator). For cross-user reads use `creators.get_for_view`."""
    return _get_profile("creator_profiles", user_id)


def get_creators_by_ids(user_ids: list[str]) -> dict[str, dict[str, Any]]:
    """Bulk lookup keyed by user_id. Returns the public-projected view
    of each creator so callers can render the resulting dict directly
    in templates without leaking internal fields. Empty input or no
    matches yields {}.

    Use this in place of dict-comprehensions like
    `{pid: profiles.get_creator_profile(pid) for pid in ids}` which
    cause N round-trips (one per id)."""
    ids = [u for u in (user_ids or []) if u]
    if not ids:
        return {}
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_profiles")
            .select("*")
            .in_("user_id", ids)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("get_creators_by_ids failed for %d ids", len(ids))
        return {}
    rows = getattr(result, "data", None) or []
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        uid = row.get("user_id")
        if uid:
            projected = public_creator(row)
            if projected is not None:
                out[str(uid)] = projected
    return out


def is_creator_onboarded(user_id: str) -> bool:
    profile = get_creator_profile(user_id)
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
