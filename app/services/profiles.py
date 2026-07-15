"""Profile data access for creator and brand onboarding.

Uses the service-role client so it bypasses RLS. Safe here because every
caller is server-side and gated by `require_role` in the route layer — by
the time we hit this module, the user_id has already been read from the
signed session cookie and the role has been checked. Never expose any of
these functions over a public endpoint without an auth check first.

**Public vs. private fields.** Owner-side reads get the full row.
Cross-user reads MUST go through `public_creator` / `public_brand` so
internal fields (`baseline_followers`, `tier`, `writing_samples`,
`verification_notes`, etc.) don't leak. Service helpers in `creators.py`
and `network.py` apply this projection.
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
# Closed vocabularies for the Phase 3 owner-private preference columns
# (migration 0016). Source of truth for the CHECK constraints and the
# route-level allow-listing. First value of each is the default that
# preserves current behavior.
DM_PREFERENCE_VALUES: tuple[str, ...] = ("open", "connections_only")
LOCATION_DISPLAY_LEVELS: tuple[str, ...] = ("city", "region", "hidden")
BABYG_TONES: tuple[str, ...] = ("casual", "professional", "direct")
BABYG_RISK_TOLERANCES: tuple[str, ...] = ("cautious", "balanced", "latitude")

# Migration 0017. Deal preferences are owner-private — never projected
# through public_creator(); they feed babyg drafts and the upcoming
# brand-outreach negotiation surface.
DEAL_USAGE_RIGHTS_VALUES: tuple[str, ...] = (
    "organic_only",
    "paid_organic",
    "paid_with_usage",
    "flexible",
)
DEAL_TRAVEL_WILLINGNESS_VALUES: tuple[str, ...] = (
    "no",
    "local_only",
    "regional",
    "open",
)


PUBLIC_CREATOR_FIELDS: tuple[str, ...] = (
    "user_id",
    "full_name",
    "instagram_handle",
    "primary_platform",
    "location_city",
    "location_region",
    "location_country",
    "niches",
    "content_formats",
    "follower_range",
    "engagement_range",
    "creator_tenure",
    "bio",
    "hard_limits",
    "onboarding_completed_at",
    # Avatar surface. profile_photo_url is the stable bucket URL;
    # updated_at is used by render sites as a `?v=` cache-buster so
    # a re-upload shows immediately even though the URL itself is
    # stable per-user.
    "profile_photo_url",
    "updated_at",
)

# Fields visible to non-operators viewing a brand. Excludes
# `verification_notes` (operator-private review notes) and any future
# internal-only flags.
PUBLIC_BRAND_FIELDS: tuple[str, ...] = (
    "user_id",
    "company_name",
    "brand_website",
    "logo_url",
    "industry",
    "is_verified",
    "verification_status",
    "verified_at",
    "location_city",
    "location_region",
    "onboarding_completed_at",
    "niche_preferences",
    "creator_size_preferences",
    "campaign_types",
    "product_description",
    "scale_descriptor",
    "model_descriptor",
    "positioning_descriptor",
    "budget_range",
)

def public_creator(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project a creator row to fields safe to render to non-owners."""
    if row is None:
        return None
    projected = {k: row.get(k) for k in PUBLIC_CREATOR_FIELDS}
    projected["location_label"] = safe_location_label(row)
    return projected


def public_brand(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """Project a brand row to fields safe to render to non-operators."""
    if row is None:
        return None
    return {k: row.get(k) for k in PUBLIC_BRAND_FIELDS}


def safe_location_label(row: dict[str, Any] | None) -> str | None:
    """Return city/region-level location text safe for public rendering.

    Honors the creator's `location_display_level` preference (Phase 3,
    migration 0016):
      * ``city``   — full label: city + region or city + country (current behavior)
      * ``region`` — drops the city; renders region/country only
      * ``hidden`` — returns None; no public location text at all

    Exact coordinates intentionally never appear in this label regardless
    of the setting; that's enforced by `PUBLIC_CREATOR_FIELDS`.
    """
    if not row:
        return None
    level = (row.get("location_display_level") or "city").strip().lower()
    if level == "hidden":
        return None
    city = _clean_location_part(row.get("location_city"))
    region = _clean_location_part(row.get("location_region"))
    country = _clean_location_part(row.get("location_country"))
    if level == "region":
        # Drop the city — region first, then country as a fallback.
        parts = [p for p in (region, country) if p]
        return ", ".join(parts[:2]) or None
    parts = [p for p in (city, region) if p]
    if len(parts) < 2 and country:
        parts.append(country)
    return ", ".join(parts[:2]) or None


# -----------------------------------------------------------------------------
# Read
# -----------------------------------------------------------------------------


def get_creator_profile(user_id: str) -> dict[str, Any] | None:
    """Owner-side full read. Caller is expected to be the profile owner
    (or an operator). For cross-user reads use `creators.get_for_view`."""
    return _get_profile("creator_profiles", user_id)


def get_brand_profile(user_id: str) -> dict[str, Any] | None:
    """Owner-side full read. For non-operator cross-user reads, project
    with `public_brand` before rendering."""
    return _get_profile("brand_profiles", user_id)


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


def _clean_location_part(value: Any) -> str:
    return " ".join(str(value or "").strip().split())
