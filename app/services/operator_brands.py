"""Operator-side brand review helpers.

All functions use the service-role client and are only called from
operator-gated routes. Public creator/brand surfaces must keep using the
projected helpers in ``profiles.py`` so operator-only notes stay private.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError
from postgrest.types import CountMethod

from app.core import supabase_client
from app.services import brand_trust

logger = logging.getLogger(__name__)

VERIFICATION_FILTERS = ("all", *brand_trust.VERIFICATION_STATUSES)


def list_brands(
    *,
    query: str | None = None,
    verification: str = "all",
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Return operator-visible brand profiles with private review fields."""
    try:
        q = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .select("*")
            .order("updated_at", desc=True)
            .limit(limit)
        )
        if verification in brand_trust.VERIFICATION_STATUSES:
            q = q.eq("verification_status", verification)
        result = q.execute()
    except PostgrestAPIError:
        logger.exception("operator brand list failed")
        return []

    rows = getattr(result, "data", None) or []
    needle = " ".join(str(query or "").strip().lower().split())
    if needle:
        rows = [
            row
            for row in rows
            if needle
            in " ".join(
                str(row.get(key) or "").lower()
                for key in (
                    "company_name",
                    "brand_website",
                    "industry",
                    "contact_full_name",
                    "contact_title",
                )
            )
        ]
    return [_with_review_status(row) for row in rows]


def get_brand(user_id: str) -> dict[str, Any] | None:
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
        logger.exception("operator brand get failed: %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    return _with_review_status(rows[0]) if rows else None


def update_verification(
    *,
    user_id: str,
    action: str,
    note: str | None,
    operator_id: str | None = None,
) -> bool:
    """Update the richer brand verification fields, keeping the legacy bool synced."""
    cleaned_note = " ".join(str(note or "").split())[:1000] or None
    status = brand_trust.clean_status(action)
    if action not in brand_trust.VERIFICATION_STATUSES:
        return False
    now = _now_iso()
    payload: dict[str, Any] = {
        "verification_status": status,
        "is_verified": status == "verified",
        "verified_at": now if status == "verified" else None,
        "verified_by_operator_id": operator_id if status == "verified" else None,
        "trust_updated_at": now,
        "verification_notes": cleaned_note,
    }

    try:
        result = (
            supabase_client.get_service_client()
            .table("brand_profiles")
            .update(payload)
            .eq("user_id", user_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("operator brand verification update failed: %s", user_id)
        return False
    ok = bool(getattr(result, "data", None))
    if ok:
        brand_trust.record_check(
            brand_user_id=user_id,
            check_type="operator_review",
            result_status=_status_to_check_result(status),
            details={"verification_status": status},
            created_by_user_id=operator_id,
            created_by_role="operator",
        )
    return ok


def list_brand_campaigns(*, limit: int = 200) -> list[dict[str, Any]]:
    """Brand-created opportunities stored in creator_job_listings."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .select("*")
            .eq("poster_role", "brand")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("operator brand campaign list failed")
        return []
    return getattr(result, "data", None) or []


def brand_counts() -> dict[str, int]:
    client = supabase_client.get_service_client()
    out = {
        "total_brands": 0,
        "verified_brands": 0,
        "pending_brand_reviews": 0,
        "blocked_or_flagged_brands": 0,
        "active_brand_campaigns": 0,
    }
    try:
        out["total_brands"] = _count(client.table("brand_profiles").select(
            "user_id", count=CountMethod.exact, head=True
        ))
        out["verified_brands"] = _count(
            client.table("brand_profiles")
            .select("user_id", count=CountMethod.exact, head=True)
            .eq("verification_status", "verified")
        )
        out["pending_brand_reviews"] = _count(
            client.table("brand_profiles")
            .select("user_id", count=CountMethod.exact, head=True)
            .in_("verification_status", ["unverified", "needs_review"])
        )
        out["blocked_or_flagged_brands"] = _count(
            client.table("brand_profiles")
            .select("user_id", count=CountMethod.exact, head=True)
            .in_("verification_status", ["high_risk", "blocked"])
        )
        out["active_brand_campaigns"] = _count(
            client.table("creator_job_listings")
            .select("id", count=CountMethod.exact, head=True)
            .eq("poster_role", "brand")
            .eq("is_active", True)
            .eq("is_taken_down", False)
        )
    except PostgrestAPIError:
        logger.exception("operator brand counts failed")
    return out


def report_counts_by_brand(brand_ids: list[str]) -> dict[str, int]:
    ids = [brand_id for brand_id in brand_ids if brand_id]
    if not ids:
        return {}
    try:
        result = (
            supabase_client.get_service_client()
            .table("abuse_reports")
            .select("target_id")
            .eq("target_type", "profile")
            .in_("target_id", ids)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("operator brand report count failed")
        return {}
    out = {brand_id: 0 for brand_id in ids}
    for row in getattr(result, "data", None) or []:
        target_id = str(row.get("target_id") or "")
        if target_id in out:
            out[target_id] += 1
    return out


def _count(query: Any) -> int:
    result = query.execute()
    return int(getattr(result, "count", 0) or 0)


def _with_review_status(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    status = brand_trust.clean_status(out.get("verification_status"))
    if status == "unverified" and out.get("is_verified"):
        status = "verified"
    out["verification_status"] = status
    out["review_status"] = status
    out["trust"] = brand_trust.public_trust(out)
    out["last_activity_at"] = out.get("updated_at") or out.get("created_at")
    return out


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _status_to_check_result(status: str) -> str:
    if status in {"verified", "likely_legitimate"}:
        return "pass"
    if status in {"needs_review", "high_risk"}:
        return "warn"
    if status == "blocked":
        return "fail"
    return "inconclusive"
