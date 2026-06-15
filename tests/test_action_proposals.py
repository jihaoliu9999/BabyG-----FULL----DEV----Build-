"""Action proposal approval-state tests."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

import pytest

from app.services import action_proposals, provider_permissions


def test_create_external_proposal_sets_pending_provider_and_scopes(monkeypatch) -> None:
    inserted: dict = {}
    monkeypatch.setattr(
        action_proposals.audit,
        "record",
        lambda **kwargs: True,
    )

    def _insert(body):
        inserted.update(body)
        return {**body, "id": str(uuid4())}

    monkeypatch.setattr(action_proposals, "_insert", _insert)

    row = action_proposals.create_proposal(
        user_id="creator-1",
        action_type="gmail.send_email",
        payload={"to": "a@example.com", "body": "hello"},
        preview={"title": "send email", "body": "hello"},
        idempotency_key="fixed-key",
    )

    assert row is not None
    assert inserted["status"] == "pending"
    assert inserted["action_category"] == "external_write"
    assert inserted["provider"] == "google"
    assert inserted["required_scopes"] == [action_proposals.GMAIL_SEND_SCOPE]
    assert inserted["idempotency_key"] == "fixed-key"


def test_money_actions_are_never_supported(monkeypatch) -> None:
    monkeypatch.setattr(action_proposals, "_insert", lambda body: body)

    with pytest.raises(action_proposals.ProhibitedActionError):
        action_proposals.create_proposal(
            user_id="creator-1",
            action_type="payment.charge",
            payload={},
            preview={},
        )


def test_unknown_non_money_action_is_rejected(monkeypatch) -> None:
    monkeypatch.setattr(action_proposals, "_insert", lambda body: body)

    with pytest.raises(action_proposals.UnknownActionError):
        action_proposals.create_proposal(
            user_id="creator-1",
            action_type="gmail.forward_to_space",
            payload={},
            preview={},
        )


def test_confirm_external_proposal_requires_provider_scope(monkeypatch) -> None:
    proposal_id = str(uuid4())
    failed: dict[str, str | None] = {}

    monkeypatch.setattr(
        action_proposals,
        "get_for_user",
        lambda *, proposal_id, user_id: {
            "id": proposal_id,
            "user_id": user_id,
            "status": "pending",
            "action_type": "gmail.send_email",
            "provider": "google",
            "required_scopes": [action_proposals.GMAIL_SEND_SCOPE],
        },
    )
    monkeypatch.setattr(
        action_proposals.provider_permissions,
        "check_provider_access",
        lambda **kwargs: provider_permissions.ProviderPermissionCheck(
            ok=False,
            reason="missing_required_scope",
            missing_scopes=(action_proposals.GMAIL_SEND_SCOPE,),
        ),
    )

    def _mark_failed(**kwargs):
        failed.update(kwargs)
        return True

    monkeypatch.setattr(action_proposals, "mark_failed", _mark_failed)

    assert (
        action_proposals.confirm_proposal(
            proposal_id=proposal_id, user_id="creator-1"
        )
        is False
    )
    assert failed["error_code"] == "missing_required_scope"
    assert failed["error_message"] == action_proposals.GMAIL_SEND_SCOPE


def test_confirm_updates_pending_exactly_once(monkeypatch) -> None:
    proposal_id = str(uuid4())
    stored_status = {"value": "pending"}
    monkeypatch.setattr(action_proposals.audit, "record", lambda **kwargs: True)
    monkeypatch.setattr(
        action_proposals,
        "get_for_user",
        lambda *, proposal_id, user_id: {
            "id": proposal_id,
            "user_id": user_id,
            "status": "pending",
            "action_type": "babyg.create_booking",
            "required_scopes": [],
        },
    )

    def _update_status(*, status, expected_status, **kwargs):
        if expected_status is not None and stored_status["value"] != expected_status:
            return False
        stored_status["value"] = status
        return True

    monkeypatch.setattr(action_proposals, "_update_status", _update_status)

    first = action_proposals.confirm_proposal(
        proposal_id=proposal_id, user_id="creator-1"
    )
    second = action_proposals.confirm_proposal(
        proposal_id=proposal_id, user_id="creator-1"
    )

    assert first is True
    assert second is False
    assert stored_status["value"] == "confirmed"


def test_execute_lock_and_executed_transition_are_single_status_steps(
    monkeypatch,
) -> None:
    calls: list[dict] = []

    def _update_status(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(action_proposals, "_update_status", _update_status)
    proposal_id = str(uuid4())

    assert action_proposals.mark_executing(
        proposal_id=proposal_id, user_id="creator-1"
    )
    assert action_proposals.mark_executed(
        proposal_id=proposal_id,
        user_id="creator-1",
        external_result_id="gmail-msg-1",
    )

    assert calls[0]["status"] == "executing"
    assert calls[0]["expected_status"] == "confirmed"
    assert calls[1]["status"] == "executed"
    assert calls[1]["expected_status"] == "executing"
    assert calls[1]["extra"]["external_result_id"] == "gmail-msg-1"


def test_cancel_only_moves_pending_to_cancelled(monkeypatch) -> None:
    captured: dict = {}

    def _update_status(**kwargs):
        captured.update(kwargs)
        return True

    monkeypatch.setattr(action_proposals, "_update_status", _update_status)

    assert action_proposals.cancel_proposal(
        proposal_id=str(uuid4()), user_id="creator-1"
    )
    assert captured["status"] == "cancelled"
    assert captured["expected_status"] == "pending"


def test_current_bot_tools_do_not_expose_external_writes() -> None:
    from app.services import prompts

    tool_names = {tool["name"] for tool in prompts.BOT_TOOL_DEFINITIONS}
    assert tool_names.isdisjoint(action_proposals.EXTERNAL_WRITE_ACTIONS)
    assert "create_booking" in tool_names


@dataclass
class _PermissionConnection:
    scopes: list[str]

    def get(self, key: str, default=None):
        return getattr(self, key, default)


def test_provider_permission_checks_google_scopes(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_permissions.oauth_connections,
        "get_google_connection",
        lambda user_id: _PermissionConnection(scopes=[action_proposals.GMAIL_SEND_SCOPE]),
    )

    ok = provider_permissions.check_provider_access(
        user_id="creator-1",
        provider="google",
        required_scopes=[action_proposals.GMAIL_SEND_SCOPE],
    )
    missing = provider_permissions.check_provider_access(
        user_id="creator-1",
        provider="google",
        required_scopes=[action_proposals.GMAIL_MODIFY_SCOPE],
    )

    assert ok.ok is True
    assert missing.ok is False
    assert missing.reason == "missing_required_scope"
    assert missing.missing_scopes == (action_proposals.GMAIL_MODIFY_SCOPE,)
