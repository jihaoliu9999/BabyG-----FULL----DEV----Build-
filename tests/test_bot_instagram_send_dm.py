"""Tests for the IG-DM send-approval flow in app/services/bot.py.

Mirrors the shape of the gmail send confirm/cancel path — the creator
taps 'confirm' on the staged proposal card, bot.confirm_action routes
to _confirm_instagram_send_dm_action, which walks the action_proposals
row through pending -> confirmed -> executing -> executed/failed and
calls Meta's Send API in between.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import bot


class _FakeMessage:
    def __init__(self, msg_id: str, tool_calls: dict[str, Any]) -> None:
        self.data = {
            "id": msg_id,
            "role": "assistant",
            "content": "drafted a reply for your instagram dm thread.",
            "tool_calls": tool_calls,
        }


@pytest.fixture
def message_row() -> dict[str, Any]:
    tool_calls = {
        "kind": "proposed_action",
        "action_type": "instagram.send_dm",
        "proposal_id": "prop-1",
        "status": "pending",
        "payload": {"ig_thread_id": "babyg-thread-1", "body": "thanks!"},
        "preview": {"title": "reply to instagram dm", "body": "thanks!"},
        "result": None,
    }
    return {
        "id": "msg-1",
        "role": "assistant",
        "content": "drafted a reply for your instagram dm thread.",
        "tool_calls": tool_calls,
    }


def _wire_common(monkeypatch, message_row: dict[str, Any]):
    """Wire the module-level helpers to trivial no-ops so we can focus
    on the branching we care about."""
    monkeypatch.setattr(
        bot, "_get_message_for_user",
        lambda *, message_id, user_id: message_row,
    )
    updates: list[dict[str, Any]] = []
    monkeypatch.setattr(
        bot, "_update_message_tool_calls",
        lambda **kw: updates.append(kw) or True,
    )
    sent_messages: list[dict[str, Any]] = []
    monkeypatch.setattr(
        bot, "create_message",
        lambda **kw: sent_messages.append(kw) or "reply-msg",
    )
    return updates, sent_messages


def test_confirm_ig_send_dm_success(monkeypatch, message_row) -> None:
    updates, sent = _wire_common(monkeypatch, message_row)
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        bot.action_proposals, "confirm_proposal",
        lambda **kw: calls.setdefault("confirm", kw) or True,
    )
    monkeypatch.setattr(
        bot.action_proposals, "mark_executing",
        lambda **kw: calls.setdefault("executing", kw) or True,
    )
    monkeypatch.setattr(
        bot.action_proposals, "mark_executed",
        lambda **kw: calls.setdefault("executed", kw) or True,
    )
    monkeypatch.setattr(
        bot.action_proposals, "mark_failed",
        lambda **kw: pytest.fail("must not mark_failed on success"),
    )
    monkeypatch.setattr(
        bot, "_load_ig_thread_for_send",
        lambda *, user_id, thread_uuid: {
            "id": thread_uuid, "ig_peer_user_id": "peer-9"
        },
    )
    monkeypatch.setattr(
        bot.oauth_connections, "get_instagram_connection",
        lambda uid: {"provider_account_id": "ig-biz-1"},
    )
    monkeypatch.setattr(
        bot.oauth_connections, "instagram_account_id",
        lambda c: (c or {}).get("provider_account_id"),
    )
    monkeypatch.setattr(
        bot.oauth_connections, "access_token_for_instagram",
        lambda uid: "TOK",
    )
    sent_calls: list[dict[str, Any]] = []

    def _send(token, *, ig_business_account_id, recipient_ig_user_id, body):
        sent_calls.append(
            {"token": token, "ig": ig_business_account_id,
             "peer": recipient_ig_user_id, "body": body}
        )
        return "meta-mid-1"

    monkeypatch.setattr(bot.instagram_meta, "send_direct_message", _send)

    result = bot.confirm_action(user_id="u1", message_id="msg-1")

    assert result.executed is True
    assert result.record_id == "meta-mid-1"
    assert result.action_type == "instagram.send_dm"
    assert calls["executed"]["external_result_id"] == "meta-mid-1"
    assert sent_calls == [
        {"token": "TOK", "ig": "ig-biz-1", "peer": "peer-9", "body": "thanks!"}
    ]
    # Two updates land: 'executing', then 'executed'. Both include the
    # rewritten tool_calls status.
    statuses = [u["tool_calls"]["status"] for u in updates]
    assert statuses == ["executing", "executed"]


def test_confirm_ig_send_dm_missing_proposal_id(monkeypatch, message_row) -> None:
    message_row["tool_calls"]["proposal_id"] = ""
    _wire_common(monkeypatch, message_row)
    monkeypatch.setattr(
        bot.action_proposals, "confirm_proposal",
        lambda **kw: pytest.fail("must not touch action_proposals"),
    )
    result = bot.confirm_action(user_id="u1", message_id="msg-1")
    assert result.executed is False
    assert "missing its approval record" in result.message


def test_confirm_ig_send_dm_proposal_already_handled(
    monkeypatch, message_row
) -> None:
    updates, sent = _wire_common(monkeypatch, message_row)
    monkeypatch.setattr(
        bot.action_proposals, "confirm_proposal", lambda **kw: False
    )
    monkeypatch.setattr(
        bot.action_proposals, "get_for_user",
        lambda **kw: {"status": "expired", "error_code": "expired"},
    )
    monkeypatch.setattr(
        bot.instagram_meta, "send_direct_message",
        lambda *a, **kw: pytest.fail("Send API must not be called on refusal"),
    )
    result = bot.confirm_action(user_id="u1", message_id="msg-1")
    assert result.executed is False
    assert "could not be approved" in result.message


def test_confirm_ig_send_dm_outside_messaging_window(
    monkeypatch, message_row
) -> None:
    updates, sent = _wire_common(monkeypatch, message_row)
    monkeypatch.setattr(bot.action_proposals, "confirm_proposal", lambda **kw: True)
    monkeypatch.setattr(bot.action_proposals, "mark_executing", lambda **kw: True)
    marked_failed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        bot.action_proposals, "mark_failed",
        lambda **kw: marked_failed.append(kw) or True,
    )
    monkeypatch.setattr(
        bot, "_load_ig_thread_for_send",
        lambda *, user_id, thread_uuid: {
            "id": thread_uuid, "ig_peer_user_id": "peer-9"
        },
    )
    monkeypatch.setattr(
        bot.oauth_connections, "get_instagram_connection",
        lambda uid: {"provider_account_id": "ig-biz-1"},
    )
    monkeypatch.setattr(
        bot.oauth_connections, "instagram_account_id",
        lambda c: (c or {}).get("provider_account_id"),
    )
    monkeypatch.setattr(
        bot.oauth_connections, "access_token_for_instagram",
        lambda uid: "TOK",
    )

    def _boom(*a, **kw):
        raise bot.instagram_meta.InstagramMessageWindowError("closed")

    monkeypatch.setattr(bot.instagram_meta, "send_direct_message", _boom)

    result = bot.confirm_action(user_id="u1", message_id="msg-1")

    assert result.executed is False
    assert "24-hour reply window" in result.message
    assert marked_failed[0]["error_code"] == "outside_messaging_window"


def test_confirm_ig_send_dm_no_token(monkeypatch, message_row) -> None:
    updates, sent = _wire_common(monkeypatch, message_row)
    monkeypatch.setattr(bot.action_proposals, "confirm_proposal", lambda **kw: True)
    monkeypatch.setattr(bot.action_proposals, "mark_executing", lambda **kw: True)
    monkeypatch.setattr(bot.action_proposals, "mark_failed", lambda **kw: True)
    monkeypatch.setattr(
        bot, "_load_ig_thread_for_send",
        lambda *, user_id, thread_uuid: {
            "id": thread_uuid, "ig_peer_user_id": "peer-9"
        },
    )
    monkeypatch.setattr(
        bot.oauth_connections, "get_instagram_connection",
        lambda uid: {"provider_account_id": "ig-biz-1"},
    )
    monkeypatch.setattr(
        bot.oauth_connections, "instagram_account_id",
        lambda c: (c or {}).get("provider_account_id"),
    )
    monkeypatch.setattr(
        bot.oauth_connections, "access_token_for_instagram",
        lambda uid: None,
    )
    monkeypatch.setattr(
        bot.instagram_meta, "send_direct_message",
        lambda *a, **kw: pytest.fail("Send API called without a token"),
    )
    result = bot.confirm_action(user_id="u1", message_id="msg-1")
    assert result.executed is False
    assert "reconnect" in result.message.lower()


def test_confirm_ig_send_dm_thread_not_found(monkeypatch, message_row) -> None:
    updates, sent = _wire_common(monkeypatch, message_row)
    monkeypatch.setattr(bot.action_proposals, "confirm_proposal", lambda **kw: True)
    monkeypatch.setattr(bot.action_proposals, "mark_executing", lambda **kw: True)
    marked_failed: list[dict[str, Any]] = []
    monkeypatch.setattr(
        bot.action_proposals, "mark_failed",
        lambda **kw: marked_failed.append(kw) or True,
    )
    monkeypatch.setattr(
        bot, "_load_ig_thread_for_send",
        lambda *, user_id, thread_uuid: None,
    )
    monkeypatch.setattr(
        bot.instagram_meta, "send_direct_message",
        lambda *a, **kw: pytest.fail("Send API called with no thread"),
    )
    result = bot.confirm_action(user_id="u1", message_id="msg-1")
    assert result.executed is False
    assert marked_failed[0]["error_code"] == "thread_not_found"


def test_cancel_ig_send_dm(monkeypatch, message_row) -> None:
    updates, sent = _wire_common(monkeypatch, message_row)
    cancelled: dict[str, Any] = {}
    monkeypatch.setattr(
        bot.action_proposals, "cancel_proposal",
        lambda **kw: cancelled.update(kw) or True,
    )
    monkeypatch.setattr(
        bot.instagram_meta, "send_direct_message",
        lambda *a, **kw: pytest.fail("Send API must not be called on cancel"),
    )
    result = bot.cancel_action(user_id="u1", message_id="msg-1")
    assert result.executed is False
    assert result.action_type == "instagram.send_dm"
    assert "cancelled" in result.message.lower()
    assert cancelled["proposal_id"] == "prop-1"
    # tool_calls got flipped to cancelled.
    assert updates[-1]["tool_calls"]["status"] == "cancelled"
