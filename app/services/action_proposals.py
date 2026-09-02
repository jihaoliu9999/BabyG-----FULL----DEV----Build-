"""Dedicated approval state for babyg actions.

External provider writes must be staged here first. Bot tools may create
proposals, but execution belongs to trusted server confirmation/executor paths.
"""

from __future__ import annotations

import logging
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import audit, provider_permissions

logger = logging.getLogger(__name__)

ProposalStatus = Literal[
    "pending",
    "confirmed",
    "executing",
    "executed",
    "failed",
    "cancelled",
    "expired",
]
ActionCategory = Literal["local_write", "external_write"]

STATUSES: tuple[ProposalStatus, ...] = (
    "pending",
    "confirmed",
    "executing",
    "executed",
    "failed",
    "cancelled",
    "expired",
)

LOCAL_WRITE_ACTIONS = {
    "babyg.create_booking": (),
    "babyg.create_content_reminder": (),
    "babyg.submit_creator_listing": (),
    "babyg.create_note": (),
    "babyg.create_task": (),
}

GOOGLE_CALENDAR_EVENTS_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"

EXTERNAL_WRITE_ACTIONS: dict[str, tuple[str, tuple[str, ...]]] = {
    "gmail.create_draft": ("google", (GMAIL_COMPOSE_SCOPE,)),
    "gmail.send_email": ("google", (GMAIL_SEND_SCOPE,)),
    "gmail.send_draft": ("google", (GMAIL_SEND_SCOPE,)),
    "gmail.modify_labels": ("google", (GMAIL_MODIFY_SCOPE,)),
    "calendar.create_event": ("google", (GOOGLE_CALENDAR_EVENTS_SCOPE,)),
    "calendar.update_event": ("google", (GOOGLE_CALENDAR_EVENTS_SCOPE,)),
    "calendar.delete_event": ("google", (GOOGLE_CALENDAR_EVENTS_SCOPE,)),
    "instagram.send_dm": ("instagram", ()),
    "external_dm.send": ("external_dm", ()),
    "restaurant.request_booking": ("restaurant", ()),
    "connected_platform.update_data": ("connected_platform", ()),
}

PROHIBITED_ACTION_TYPES = {
    "payment.charge",
    "payment.refund",
    "payment.transfer",
    "payment.authorize",
    "purchase.buy",
    "ads.buy",
    "credential.collect_payment",
    "restaurant.prepaid_booking",
}
PROHIBITED_PREFIXES = ("payment.", "purchase.", "financial.", "funds.", "money.")
PROHIBITED_TERMS = (
    "payment",
    "purchase",
    "charge",
    "card",
    "credential",
    "transfer",
    "funds",
    "paid_booking",
)


class ActionProposalError(ValueError):
    """Base validation error for action proposals."""


class ProhibitedActionError(ActionProposalError):
    """Raised when an action would involve money/payment custody."""


class UnknownActionError(ActionProposalError):
    """Raised for action types outside the approved taxonomy."""


def create_proposal(
    *,
    user_id: str,
    action_type: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    source_message_id: str | None = None,
    provider: str | None = None,
    required_scopes: list[str] | tuple[str, ...] | None = None,
    expires_in_seconds: int = 60 * 60,
    idempotency_key: str | None = None,
) -> dict[str, Any] | None:
    """Create a pending action proposal.

    One proposal represents one action. Money/payment action types are
    permanently rejected.
    """
    action = _validate_action_type(action_type)
    category = action_category(action)
    resolved_provider = provider
    resolved_scopes = tuple(required_scopes or ())
    if category == "external_write":
        default_provider, default_scopes = EXTERNAL_WRITE_ACTIONS[action]
        resolved_provider = resolved_provider or default_provider
        resolved_scopes = resolved_scopes or default_scopes
    expires_at = datetime.now(UTC) + timedelta(seconds=max(expires_in_seconds, 1))
    body: dict[str, Any] = {
        "user_id": user_id,
        "source_message_id": safe_uuid(source_message_id) if source_message_id else None,
        "action_type": action,
        "action_category": category,
        "provider": resolved_provider,
        "required_scopes": list(resolved_scopes),
        "payload": payload,
        "preview": preview,
        "status": "pending",
        "idempotency_key": idempotency_key or _new_idempotency_key(action),
        "expires_at": expires_at.isoformat(),
    }
    row = _insert(body)
    if row:
        audit.record(
            actor_user_id=user_id,
            action="action_proposal.created",
            target_type="action_proposal",
            target_id=str(row.get("id")),
            notes=action,
        )
    return row


def get_for_user(*, proposal_id: str, user_id: str) -> dict[str, Any] | None:
    pid = safe_uuid(proposal_id)
    if not pid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("action_proposals")
            .select("*")
            .eq("id", pid)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("action proposal lookup failed: %s", proposal_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def list_pending_for_user(*, user_id: str, limit: int = 10) -> list[dict[str, Any]]:
    """Non-expired pending proposals for the home surface.

    Expired rows never survive `confirm_proposal` (it flips them to
    expired first), but they can still sit at status='pending' in the
    table until a creator touches them. Filter client-side so a stale
    Gmail draft doesn't clutter the home rail — the confirm path is
    the authority on state transitions, this is a read.
    """
    capped = max(1, min(int(limit), 50))
    try:
        result = (
            supabase_client.get_service_client()
            .table("action_proposals")
            .select("id,action_type,action_category,provider,preview,expires_at,created_at,source_message_id")
            .eq("user_id", user_id)
            .eq("status", "pending")
            .order("created_at", desc=True)
            .limit(capped)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("action proposal list failed: %s", user_id)
        return []
    rows = list(getattr(result, "data", None) or [])
    fresh = [r for r in rows if not _is_expired(r)]
    return fresh


def count_pending_for_user(*, user_id: str) -> int:
    """Fast count for tab badges / needs_count summary."""
    return len(list_pending_for_user(user_id=user_id, limit=50))


def confirm_proposal(*, proposal_id: str, user_id: str) -> bool:
    """Creator approval step. Moves pending -> confirmed exactly once."""
    row = get_for_user(proposal_id=proposal_id, user_id=user_id)
    if not row or row.get("status") != "pending":
        return False
    if _is_expired(row):
        expire_proposal(proposal_id=proposal_id, user_id=user_id)
        return False
    action = _validate_action_type(str(row.get("action_type") or ""))
    if action_category(action) == "external_write":
        permission = provider_permissions.check_provider_access(
            user_id=user_id,
            provider=str(row.get("provider") or ""),
            required_scopes=_as_scope_list(row.get("required_scopes")),
        )
        if not permission.ok:
            mark_failed(
                proposal_id=proposal_id,
                user_id=user_id,
                error_code=permission.reason or "provider_permission_failed",
                error_message=", ".join(permission.missing_scopes) or None,
            )
            return False
    updated = _update_status(
        proposal_id=proposal_id,
        user_id=user_id,
        status="confirmed",
        expected_status="pending",
        extra={"confirmed_at": _now_iso()},
    )
    if updated:
        audit.record(
            actor_user_id=user_id,
            action="action_proposal.confirmed",
            target_type="action_proposal",
            target_id=proposal_id,
            notes=action,
        )
    return updated


def mark_executing(*, proposal_id: str, user_id: str) -> bool:
    """Executor lock. Moves confirmed -> executing exactly once."""
    return _update_status(
        proposal_id=proposal_id,
        user_id=user_id,
        status="executing",
        expected_status="confirmed",
        extra={"executing_at": _now_iso()},
    )


def mark_executed(
    *, proposal_id: str, user_id: str, external_result_id: str | None = None
) -> bool:
    return _update_status(
        proposal_id=proposal_id,
        user_id=user_id,
        status="executed",
        expected_status="executing",
        extra={"executed_at": _now_iso(), "external_result_id": external_result_id},
    )


def mark_failed(
    *,
    proposal_id: str,
    user_id: str,
    error_code: str,
    error_message: str | None = None,
) -> bool:
    return _update_status(
        proposal_id=proposal_id,
        user_id=user_id,
        status="failed",
        expected_status=None,
        extra={
            "failed_at": _now_iso(),
            "error_code": error_code[:120],
            "error_message": error_message[:1000] if error_message else None,
        },
    )


def cancel_proposal(*, proposal_id: str, user_id: str) -> bool:
    return _update_status(
        proposal_id=proposal_id,
        user_id=user_id,
        status="cancelled",
        expected_status="pending",
        extra={"cancelled_at": _now_iso()},
    )


def expire_proposal(*, proposal_id: str, user_id: str) -> bool:
    return _update_status(
        proposal_id=proposal_id,
        user_id=user_id,
        status="expired",
        expected_status="pending",
        extra={"failed_at": _now_iso(), "error_code": "expired"},
    )


def action_category(action_type: str) -> ActionCategory:
    action = _validate_action_type(action_type)
    if action in LOCAL_WRITE_ACTIONS:
        return "local_write"
    return "external_write"


def is_external_action(action_type: str) -> bool:
    return action_category(action_type) == "external_write"


def is_prohibited_action(action_type: str) -> bool:
    normalized = _normalize_action_type(action_type)
    if normalized in PROHIBITED_ACTION_TYPES:
        return True
    if normalized.startswith(PROHIBITED_PREFIXES):
        return True
    return any(term in normalized for term in PROHIBITED_TERMS)


def _validate_action_type(action_type: str) -> str:
    normalized = _normalize_action_type(action_type)
    if is_prohibited_action(normalized):
        raise ProhibitedActionError("money/payment actions are never supported")
    if normalized in LOCAL_WRITE_ACTIONS or normalized in EXTERNAL_WRITE_ACTIONS:
        return normalized
    raise UnknownActionError(f"unknown action type: {normalized}")


def _insert(body: dict[str, Any]) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("action_proposals")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("action proposal insert failed")
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def _update_status(
    *,
    proposal_id: str,
    user_id: str,
    status: ProposalStatus,
    expected_status: str | None,
    extra: dict[str, Any] | None = None,
) -> bool:
    pid = safe_uuid(proposal_id)
    if not pid:
        return False
    payload: dict[str, Any] = {"status": status, **(extra or {})}
    try:
        query = (
            supabase_client.get_service_client()
            .table("action_proposals")
            .update(payload)
            .eq("id", pid)
            .eq("user_id", user_id)
        )
        if expected_status is not None:
            query = query.eq("status", expected_status)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("action proposal status update failed: %s", proposal_id)
        return False
    return bool(getattr(result, "data", None))


def _as_scope_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(scope) for scope in value if str(scope).strip()]


def _is_expired(row: dict[str, Any]) -> bool:
    raw = row.get("expires_at")
    if not raw:
        return False
    try:
        expires_at = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC)


def _normalize_action_type(action_type: str) -> str:
    return (action_type or "").strip().lower()


def _new_idempotency_key(action_type: str) -> str:
    return f"{action_type}:{secrets.token_urlsafe(24)}"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()
