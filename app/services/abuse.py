"""Abuse reports service — user submissions + operator review queue.

Schema: see migrations/0002_schema.sql §abuse_reports. The CHECK on
target_type allows ('profile', 'listing', 'dm_thread', 'message') and
status is ('pending', 'dismissed', 'actioned', 'escalated').

Service-role client (RLS bypass). Routes always auth-gate first.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError
from postgrest.types import CountMethod

from app.core import supabase_client

logger = logging.getLogger(__name__)


TARGET_TYPES = ("profile", "listing", "dm_thread", "message")
STATUSES = ("pending", "dismissed", "actioned", "escalated")
RESOLUTION_ACTIONS = ("dismiss", "action", "escalate")
ACTION_TO_STATUS = {
    "dismiss": "dismissed",
    "action": "actioned",
    "escalate": "escalated",
}


def create_report(
    *,
    reporter_id: str,
    target_type: str,
    target_id: str,
    reason: str,
) -> bool:
    if target_type not in TARGET_TYPES:
        return False
    reason = (reason or "").strip()[:1000]
    if len(reason) < 10:
        return False
    payload = {
        "reporter_id": reporter_id,
        "target_type": target_type,
        "target_id": target_id,
        "reason": reason,
    }
    try:
        supabase_client.get_service_client().table("abuse_reports").insert(
            payload
        ).execute()
    except PostgrestAPIError:
        logger.exception("abuse_reports insert failed")
        return False
    return True


def list_by_status(
    status: str | None = None, *, limit: int = 100
) -> list[dict[str, Any]]:
    try:
        query = (
            supabase_client.get_service_client()
            .table("abuse_reports")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
        )
        if status and status in STATUSES:
            query = query.eq("status", status)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("abuse list failed: status=%s", status)
        return []
    return getattr(result, "data", None) or []


def get(report_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("abuse_reports")
            .select("*")
            .eq("id", report_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("abuse get failed: %s", report_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def resolve(
    *, report_id: str, reviewer_id: str, action: str, notes: str | None
) -> bool:
    if action not in ACTION_TO_STATUS:
        return False
    new_status = ACTION_TO_STATUS[action]
    if new_status in ("actioned", "escalated") and not (notes or "").strip():
        return False
    payload: dict[str, Any] = {
        "status": new_status,
        "reviewed_by": reviewer_id,
        "reviewed_at": _now_iso(),
        "action_notes": (notes or "").strip()[:1000] or None,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("abuse_reports")
            .update(payload)
            .eq("id", report_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("abuse resolve failed: %s", report_id)
        return False
    return bool(getattr(result, "data", None))


def count_pending() -> int:
    try:
        result = (
            supabase_client.get_service_client()
            .table("abuse_reports")
            .select("id", count=CountMethod.exact)
            .eq("status", "pending")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("abuse count_pending failed")
        return 0
    return int(getattr(result, "count", 0) or 0)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
