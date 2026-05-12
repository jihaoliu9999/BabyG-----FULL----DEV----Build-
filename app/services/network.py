"""Creator network service — directory + connections.

The `creator_connections` schema is directional (requester_id,
addressee_id, status). A pending row from A→B and another from B→A would
both pass the UNIQUE constraint, so we explicitly check both directions
before inserting a request — one outstanding request per ordered pair.

Status values (CHECK in 0002_schema.sql §creator_connections):
  pending | accepted | declined | blocked

Only the addressee may flip pending → accepted/declined.
Either side may set the row to blocked.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services.profiles import public_creator

logger = logging.getLogger(__name__)


STATUSES = ("pending", "accepted", "declined", "blocked")
RESPOND_ACTIONS = ("accept", "decline", "block")
ACTION_TO_STATUS = {"accept": "accepted", "decline": "declined", "block": "blocked"}


# -----------------------------------------------------------------------------
# Directory
# -----------------------------------------------------------------------------


def list_directory_for_creator(user_id: str) -> list[dict[str, Any]]:
    """Onboarded creators visible to `user_id`.

    Excludes:
      * self
      * anyone the current user blocked (rows where requester=user, status=blocked)
      * anyone who blocked the current user (rows where addressee=user, status=blocked)

    Declined and pending relationships do NOT hide the peer — they're
    still visible in the directory, but with a CTA that reflects the
    state.
    """
    rows = _list_onboarded_creators()
    blocked = _blocked_user_ids(user_id)
    filtered = [
        r for r in rows
        if r.get("user_id") != user_id and r.get("user_id") not in blocked
    ]
    return [p for p in (public_creator(r) for r in filtered) if p is not None]


def _list_onboarded_creators() -> list[dict[str, Any]]:
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
        logger.exception("network directory list failed")
        return []
    return getattr(result, "data", None) or []


def _blocked_user_ids(user_id: str) -> set[str]:
    """User IDs the caller can't see (either side blocked the other)."""
    uid = safe_uuid(user_id)
    if not uid:
        return set()
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_connections")
            .select("requester_id, addressee_id, status")
            .or_(f"requester_id.eq.{uid},addressee_id.eq.{uid}")
            .eq("status", "blocked")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("blocked lookup failed for %s", user_id)
        return set()
    rows = getattr(result, "data", None) or []
    out: set[str] = set()
    for r in rows:
        peer = r["addressee_id"] if r["requester_id"] == user_id else r["requester_id"]
        out.add(str(peer))
    return out


# -----------------------------------------------------------------------------
# Connection state
# -----------------------------------------------------------------------------


def get_connection_between(a: str, b: str) -> dict[str, Any] | None:
    """Return any existing connection row between (a, b) regardless of
    direction. Returns the most recently updated row if multiple exist
    (which shouldn't happen — request_connection prevents it)."""
    a_safe, b_safe = safe_uuid(a), safe_uuid(b)
    if not a_safe or not b_safe:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_connections")
            .select("*")
            .or_(
                f"and(requester_id.eq.{a_safe},addressee_id.eq.{b_safe}),"
                f"and(requester_id.eq.{b_safe},addressee_id.eq.{a_safe})"
            )
            .order("requested_at", desc=True)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("get_connection_between failed: %s/%s", a, b)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def request_connection(*, requester_id: str, addressee_id: str) -> bool:
    if requester_id == addressee_id:
        return False

    existing = get_connection_between(requester_id, addressee_id)
    if existing is not None:
        status = existing.get("status")
        if status in {"pending", "accepted", "blocked"}:
            # Outstanding request, already connected, or blocked. No-op.
            return False
        if status == "declined":
            # The UNIQUE constraint is on (requester_id, addressee_id),
            # so a fresh insert in either direction would collide. Flip
            # the existing row back to pending and re-orient it so the
            # current requester is the requester. responded_at is cleared
            # so the addressee doesn't see "responded N days ago" text.
            try:
                supabase_client.get_service_client().table(
                    "creator_connections"
                ).update(
                    {
                        "requester_id": requester_id,
                        "addressee_id": addressee_id,
                        "status": "pending",
                        "requested_at": _now_iso(),
                        "responded_at": None,
                    }
                ).eq("id", existing["id"]).execute()
            except PostgrestAPIError:
                logger.exception(
                    "connection re-request failed: %s -> %s",
                    requester_id, addressee_id,
                )
                return False
            return True

    try:
        supabase_client.get_service_client().table("creator_connections").insert(
            {
                "requester_id": requester_id,
                "addressee_id": addressee_id,
                "status": "pending",
            }
        ).execute()
    except PostgrestAPIError:
        logger.exception(
            "connection request failed: %s -> %s", requester_id, addressee_id
        )
        return False
    return True


def respond_to_connection(
    *, connection_id: str, responder_id: str, action: str
) -> bool:
    if action not in ACTION_TO_STATUS:
        return False
    new_status = ACTION_TO_STATUS[action]

    row = _get_connection(connection_id)
    if row is None:
        return False

    # accept/decline are addressee-only. block can come from either side.
    if action in ("accept", "decline") and row["addressee_id"] != responder_id:
        return False
    if action == "block" and responder_id not in (row["addressee_id"], row["requester_id"]):
        return False

    payload: dict[str, Any] = {
        "status": new_status,
        "responded_at": _now_iso(),
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_connections")
            .update(payload)
            .eq("id", connection_id)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("respond_to_connection failed: %s", connection_id)
        return False
    return bool(getattr(result, "data", None))


def _get_connection(connection_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_connections")
            .select("*")
            .eq("id", connection_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("get_connection failed: %s", connection_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


# -----------------------------------------------------------------------------
# Lists for the connections page
# -----------------------------------------------------------------------------


def list_accepted_for_user(user_id: str) -> list[dict[str, Any]]:
    """Mutually accepted connections. Each row carries `peer_id`."""
    rows = _list_for_user(user_id, status="accepted")
    return _annotate_peer(rows, user_id)


def list_incoming_pending(user_id: str) -> list[dict[str, Any]]:
    rows = _list_for_user(user_id, status="pending", as_addressee=True)
    return _annotate_peer(rows, user_id)


def list_outgoing_pending(user_id: str) -> list[dict[str, Any]]:
    rows = _list_for_user(user_id, status="pending", as_addressee=False)
    return _annotate_peer(rows, user_id)


def _list_for_user(
    user_id: str, *, status: str, as_addressee: bool | None = None
) -> list[dict[str, Any]]:
    uid = safe_uuid(user_id)
    if not uid:
        return []
    try:
        query = (
            supabase_client.get_service_client()
            .table("creator_connections")
            .select("*")
            .eq("status", status)
        )
        if as_addressee is True:
            query = query.eq("addressee_id", uid)
        elif as_addressee is False:
            query = query.eq("requester_id", uid)
        else:
            query = query.or_(
                f"requester_id.eq.{uid},addressee_id.eq.{uid}"
            )
        result = query.order("requested_at", desc=True).limit(500).execute()
    except PostgrestAPIError:
        logger.exception("connection list failed: %s status=%s", user_id, status)
        return []
    return getattr(result, "data", None) or []


def _annotate_peer(
    rows: list[dict[str, Any]], user_id: str
) -> list[dict[str, Any]]:
    for r in rows:
        r["peer_id"] = (
            r["addressee_id"] if r["requester_id"] == user_id else r["requester_id"]
        )
    return rows


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
