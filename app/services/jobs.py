"""Creator job listings — collab posts, UGC swaps, hiring asks, creator shares.

Schema: see migrations/0002_schema.sql §creator_job_listings. Listings are
written by creators only; creators browse and respond through the network.

Operators can take down a listing with a reason (`is_taken_down=true,
taken_down_reason, taken_down_at, taken_down_by`).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)


LISTING_TYPES = ("collab", "hiring", "brand_deal")


def list_active(*, niche: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Active, non-taken-down listings, newest first. Optionally filter by
    a niche (any overlap with target_niches)."""
    try:
        query = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .select("*")
            .eq("is_active", True)
            .eq("is_taken_down", False)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if niche:
            query = query.contains("target_niches", [niche])
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("jobs list_active failed")
        return []
    return getattr(result, "data", None) or []


def list_by_poster(poster_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .select("*")
            .eq("poster_user_id", poster_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs list_by_poster failed: %s", poster_id)
        return []
    return getattr(result, "data", None) or []


def list_for_operator(*, taken_down: bool | None = None) -> list[dict[str, Any]]:
    try:
        query = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .select("*")
            .order("created_at", desc=True)
            .limit(200)
        )
        if taken_down is True:
            query = query.eq("is_taken_down", True)
        elif taken_down is False:
            query = query.eq("is_taken_down", False)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("jobs list_for_operator failed")
        return []
    return getattr(result, "data", None) or []


def get(listing_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .select("*")
            .eq("id", listing_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs get failed: %s", listing_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def create(*, poster_id: str, payload: dict[str, Any]) -> str | None:
    body = {**payload, "poster_user_id": poster_id}
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs create failed")
        return None
    rows = getattr(result, "data", None) or []
    return str(rows[0]["id"]) if rows else None


def update(
    listing_id: str, payload: dict[str, Any], *, poster_id: str
) -> bool:
    """Update a listing. `poster_id` is REQUIRED and applied as a
    service-layer write filter so a route-layer slip can't mutate
    another user's listing. Operators don't write through this —
    they use `take_down`.
    """
    if not payload:
        return True
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .update(payload)
            .eq("id", listing_id)
            .eq("poster_user_id", poster_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs update failed: %s", listing_id)
        return False
    return bool(getattr(result, "data", None))


def deactivate(listing_id: str, *, poster_id: str) -> bool:
    """Poster-initiated soft-close. Sets is_active=false."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .update({"is_active": False})
            .eq("id", listing_id)
            .eq("poster_user_id", poster_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs deactivate failed: %s", listing_id)
        return False
    return bool(getattr(result, "data", None))


def delete(listing_id: str, *, poster_id: str) -> bool:
    """Poster-initiated delete, owner-filtered at the service layer."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .delete()
            .eq("id", listing_id)
            .eq("poster_user_id", poster_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs delete failed: %s", listing_id)
        return False
    return bool(getattr(result, "data", None))


def take_down(*, listing_id: str, operator_id: str, reason: str) -> bool:
    if not (reason or "").strip():
        return False
    payload = {
        "is_taken_down": True,
        "taken_down_reason": reason.strip()[:1000],
        "taken_down_by": operator_id,
        "taken_down_at": _now_iso(),
        "is_active": False,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .update(payload)
            .eq("id", listing_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("jobs take_down failed: %s", listing_id)
        return False
    return bool(getattr(result, "data", None))


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
