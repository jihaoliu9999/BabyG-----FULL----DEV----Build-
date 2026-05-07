"""Brand profile data access — read for the brand surface, queue + verify
for the operator surface.

Uses the service-role client (RLS bypass). Routes always auth-gate first.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Verification queue (operator)
# -----------------------------------------------------------------------------


def list_pending() -> list[dict[str, Any]]:
    """Brands that finished onboarding but haven't been verified yet.

    Operator-side view. Verification rejections set `verified_at = null` and
    `is_verified = false` but write `verification_notes`; we exclude those
    from the pending queue so the operator doesn't keep re-seeing rejected
    brands. The brand can re-submit via re-running onboarding (a future step
    will add an explicit "request review again" button).
    """
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .select("*")
            .eq("is_verified", False)
            .is_("verification_notes", "null")
            .not_.is_("onboarding_completed_at", "null")
            .order("onboarding_completed_at", desc=False)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand pending list failed")
        return []
    return getattr(result, "data", None) or []


def list_verified() -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .select("*")
            .eq("is_verified", True)
            .order("verified_at", desc=True)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand verified list failed")
        return []
    return getattr(result, "data", None) or []


def list_rejected() -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .select("*")
            .eq("is_verified", False)
            .not_.is_("verification_notes", "null")
            .order("updated_at", desc=True)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand rejected list failed")
        return []
    return getattr(result, "data", None) or []


def get_by_user_id(user_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .select("*")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("get brand by user_id failed: %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def verify(user_id: str, notes: str | None) -> bool:
    payload = {
        "is_verified": True,
        "verified_at": _now_iso(),
        "verification_notes": notes or None,
    }
    return _update(user_id, payload)


def reject(user_id: str, notes: str) -> bool:
    if not notes:
        return False
    payload = {
        "is_verified": False,
        "verified_at": None,
        "verification_notes": notes,
    }
    return _update(user_id, payload)


def _update(user_id: str, payload: dict[str, Any]) -> bool:
    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .update(payload)
            .eq("user_id", user_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("brand update failed for %s", user_id)
        return False
    return bool(getattr(result, "data", None))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
