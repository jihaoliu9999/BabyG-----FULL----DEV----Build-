"""Phase 2 babyg assistant scaffold tests."""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.agent.tools import read_only
from app.core.security import SESSION_COOKIE, write_session
from app.routes import creator as creator_routes
from app.services import bot as bot_service


def _signed_in(client: TestClient, *, role: str, user_id: str = "u-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def test_bot_page_requires_creator(client: TestClient) -> None:
    _signed_in(client, role="operator")
    response = client.get("/creator/bot")
    assert response.status_code == 403


def test_bot_page_redirects_until_onboarded(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: {})

    response = client.get("/creator/bot", follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "/onboarding/creator"


def test_bot_page_preserves_paragraph_breaks_in_assistant_content(
    monkeypatch, client: TestClient
) -> None:
    """Multi-paragraph assistant responses must round-trip through the template
    without losing the blank line. This protects the chat readability contract:
    Claude returns \\n\\n between paragraphs; the template + CSS render them as
    real paragraph gaps. If any layer collapses newlines, this test fails."""
    _signed_in(client, role="creator")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "list_messages",
        lambda uid: [
            {
                "role": "assistant",
                "content": "verdict: counter at six.\n\npush back on usage.",
                "flagged": False,
            },
        ],
    )

    response = client.get("/creator/bot")

    assert response.status_code == 200
    # The literal blank line between sentences must survive into the HTML.
    assert "verdict: counter at six.\n\npush back on usage." in response.text


def test_bot_page_renders_history(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "list_messages",
        lambda uid: [
            {"role": "user", "content": "Need a caption", "flagged": False},
            {"role": "assistant", "content": "Drafting it.", "flagged": False},
        ],
    )

    response = client.get("/creator/bot")

    assert response.status_code == 200
    assert "What are we working on today?" in response.text
    assert '/static/js/bot.js' in response.text
    assert "Need a caption" in response.text
    assert "Drafting it." in response.text


def test_bot_page_renders_pending_action_controls(monkeypatch, client: TestClient) -> None:
    message_id = str(uuid4())
    _signed_in(client, role="creator")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "list_messages",
        lambda uid: [
            {
                "id": message_id,
                "role": "assistant",
                "content": "I can create this local calendar item after you confirm.",
                "flagged": False,
                "tool_calls": {
                    "kind": "proposed_action",
                    "status": "pending",
                    "action_type": "create_booking",
                    "payload": {},
                    "preview": "Preview",
                    "result": None,
                },
            }
        ],
    )

    response = client.get("/creator/bot")

    assert response.status_code == 200
    assert "bot-action-card bot-action-pending" in response.text
    assert f"/creator/bot/actions/{message_id}/confirm" in response.text
    assert f"/creator/bot/actions/{message_id}/cancel" in response.text


def test_bot_post_persists_turn_and_redirects(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator", user_id="creator-1")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    calls: list[dict[str, str]] = []

    def _handle(*, user_id: str, content: str):
        calls.append({"user_id": user_id, "content": content})
        return bot_service.BotTurnResult(response="Done")

    monkeypatch.setattr(creator_routes.bot, "handle_creator_message", _handle)

    response = client.post(
        "/creator/bot",
        data={"message": "Draft a caption"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/bot"
    assert calls == [{"user_id": "creator-1", "content": "Draft a caption"}]


def test_bot_confirm_and_cancel_routes_redirect(monkeypatch, client: TestClient) -> None:
    message_id = str(uuid4())
    _signed_in(client, role="creator", user_id="creator-1")
    calls: list[dict[str, str]] = []

    def _confirm(*, user_id: str, message_id: str):
        calls.append({"action": "confirm", "user_id": user_id, "message_id": message_id})
        return bot_service.BotActionResult(message="confirmed", found=True)

    def _cancel(*, user_id: str, message_id: str):
        calls.append({"action": "cancel", "user_id": user_id, "message_id": message_id})
        return bot_service.BotActionResult(message="cancelled", found=True)

    monkeypatch.setattr(creator_routes.bot, "confirm_action", _confirm)
    monkeypatch.setattr(creator_routes.bot, "cancel_action", _cancel)

    confirm_response = client.post(
        f"/creator/bot/actions/{message_id}/confirm",
        follow_redirects=False,
    )
    cancel_response = client.post(
        f"/creator/bot/actions/{message_id}/cancel",
        follow_redirects=False,
    )

    assert confirm_response.status_code == 303
    assert cancel_response.status_code == 303
    assert confirm_response.headers["location"] == "/creator/bot"
    assert cancel_response.headers["location"] == "/creator/bot"
    assert calls == [
        {"action": "confirm", "user_id": "creator-1", "message_id": message_id},
        {"action": "cancel", "user_id": "creator-1", "message_id": message_id},
    ]


def test_normal_bot_turn_persists_user_and_assistant_messages(monkeypatch) -> None:
    created: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [{"role": "user", "content": "What should I post today?"}],
    )
    monkeypatch.setattr(
        bot_service.anthropic_client,
        "complete_chat",
        lambda **kwargs: bot_service.anthropic_client.ClaudeResponse(
            text="Post a quick Miami reset reel."
        ),
    )

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="What should I post today?",
    )

    assert result.response == "Post a quick Miami reset reel."
    assert [m["role"] for m in created] == ["user", "assistant"]
    assert created[0]["content"] == "What should I post today?"
    assert created[1]["content"] == "Post a quick Miami reset reel."


def test_normal_bot_turn_does_not_collect_full_context(monkeypatch) -> None:
    created: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [{"role": "user", "content": "thanks"}],
    )

    def _fail_collect_context(user_id: str):
        raise AssertionError("normal turns should not prefetch every read tool")

    monkeypatch.setattr(bot_service.read_only, "collect_context", _fail_collect_context)

    captured: dict[str, object] = {}

    def _fake_claude(**kwargs):
        captured.update(kwargs)
        return bot_service.anthropic_client.ClaudeResponse(
            text="Anytime.",
            stop_reason="end_turn",
        )

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    result = bot_service.handle_creator_message(user_id="creator-1", content="thanks")

    assert result.response == "Anytime."
    assert captured["tools"] == bot_service.prompts.BOT_TOOL_DEFINITIONS
    assert created[-1]["tool_calls"] is None


def test_agent_loop_executes_read_tool_only_when_requested(monkeypatch) -> None:
    created: list[dict] = []
    captured_calls: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "What's hot in food this week?"}
        ],
    )
    monkeypatch.setattr(
        bot_service.read_only,
        "read_my_profile",
        lambda uid: {"niches": ["food"], "tier": "pro"},
    )

    intel_calls: list[dict] = []

    def _read_intel_feed(*, niches, tier, limit=5):
        intel_calls.append({"niches": niches, "tier": tier, "limit": limit})
        return [{"title": "New dinner room", "category": "venue"}]

    monkeypatch.setattr(bot_service.read_only, "read_intel_feed", _read_intel_feed)

    responses = [
        bot_service.anthropic_client.ClaudeResponse(
            text="",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "read_intel_feed",
                    "input": {"limit": 7},
                }
            ],
            stop_reason="tool_use",
            input_tokens=20,
            output_tokens=5,
        ),
        bot_service.anthropic_client.ClaudeResponse(
            text="New dinner room is the one to watch.",
            content=[
                {"type": "text", "text": "New dinner room is the one to watch."}
            ],
            stop_reason="end_turn",
            input_tokens=30,
            output_tokens=8,
        ),
    ]

    def _fake_claude(**kwargs):
        captured_calls.append(kwargs)
        return responses.pop(0)

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="What's hot in food this week?",
    )

    assert result.response == "New dinner room is the one to watch."
    assert result.input_tokens == 50
    assert result.output_tokens == 13
    assert intel_calls == [{"niches": ["food"], "tier": "pro", "limit": 7}]
    assert len(captured_calls) == 2
    tool_result_message = captured_calls[1]["messages"][-1]
    assert tool_result_message["role"] == "user"
    assert tool_result_message["content"][0]["type"] == "tool_result"
    assert "New dinner room" in tool_result_message["content"][0]["content"]
    assistant_tool_calls = created[-1]["tool_calls"]
    assert assistant_tool_calls == [
        {
            "kind": "read_tool",
            "name": "read_intel_feed",
            "input": {"limit": 7},
            "ok": True,
        }
    ]


def test_missing_anthropic_key_returns_graceful_fallback(monkeypatch) -> None:
    created: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [{"role": "user", "content": "What should I post today?"}],
    )

    def _missing_key(**kwargs):
        raise bot_service.anthropic_client.ClaudeNotConfiguredError("missing")

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _missing_key)

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="What should I post today?",
    )

    assert "Claude is not configured yet" in result.response
    assert created[-1]["role"] == "assistant"
    assert created[-1]["content"] == result.response


def test_proposed_action_does_not_write_immediately(monkeypatch) -> None:
    created: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )

    def _fail_write(**kwargs):
        raise AssertionError("proposal should not write before confirmation")

    monkeypatch.setattr(bot_service.bookings, "create", _fail_write)
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "Create a booking called Brand Call"}
        ],
    )
    responses = [
        bot_service.anthropic_client.ClaudeResponse(
            text="",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_booking",
                    "name": "create_booking",
                    "input": {
                        "title": "Brand Call",
                        "type": "event",
                        "starts_at": "2099-05-08T10:00:00Z",
                        "notes": "Creator-provided local calendar item.",
                    },
                }
            ],
            stop_reason="tool_use",
        ),
        bot_service.anthropic_client.ClaudeResponse(
            text="Review the booking card below.",
            content=[{"type": "text", "text": "Review the booking card below."}],
            stop_reason="end_turn",
        ),
    ]
    monkeypatch.setattr(
        bot_service.anthropic_client,
        "complete_chat",
        lambda **kwargs: responses.pop(0),
    )

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="Create a booking called Brand Call on 2099-05-08T10:00",
    )

    assert result.response == "Review the booking card below."
    assert len(created) == 2
    proposal = created[1]["tool_calls"]
    assert proposal["kind"] == "proposed_action"
    assert proposal["status"] == "pending"
    assert proposal["action_type"] == "create_booking"


def test_booking_proposal_does_not_infer_restaurant_type(monkeypatch) -> None:
    created: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "Add restaurant dinner to my calendar"}
        ],
    )
    responses = [
        bot_service.anthropic_client.ClaudeResponse(
            text="",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_booking",
                    "name": "create_booking",
                    "input": {
                        "title": "Dinner Visit",
                        "type": "restaurant",
                        "starts_at": "2099-05-08T20:00:00Z",
                        "venue_name": "Dinner spot",
                    },
                }
            ],
            stop_reason="tool_use",
        ),
        bot_service.anthropic_client.ClaudeResponse(
            text="Review the local calendar card below.",
            content=[
                {"type": "text", "text": "Review the local calendar card below."}
            ],
            stop_reason="end_turn",
        ),
    ]
    monkeypatch.setattr(
        bot_service.anthropic_client,
        "complete_chat",
        lambda **kwargs: responses.pop(0),
    )

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="Add restaurant dinner to my calendar called Dinner Visit on 2099-05-08T20:00",
    )

    proposal = created[1]["tool_calls"]
    assert "local calendar card" in result.response
    assert proposal["action_type"] == "create_booking"
    assert proposal["payload"]["type"] == "event"


def test_confirm_writes_exactly_once_and_double_confirm_does_not_duplicate(
    monkeypatch,
) -> None:
    message_id = str(uuid4())
    tool_calls = {
        "kind": "proposed_action",
        "status": "pending",
        "action_type": "create_booking",
        "payload": {
            "title": "Brand Call",
            "type": "event",
            "starts_at": "2099-05-08T10:00",
            "ends_at": None,
            "notes": None,
            "venue_name": None,
            "status": "confirmed",
        },
        "preview": "Preview",
        "result": None,
    }
    row = {"id": message_id, "user_id": "creator-1", "tool_calls": tool_calls}
    stored_status = {"value": "pending"}
    writes: list[dict] = []
    assistant_messages: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "_get_message_for_user",
        lambda *, message_id, user_id: {
            **row,
            "tool_calls": {**tool_calls, "status": "pending"},
        }
        if user_id == "creator-1"
        else None,
    )

    def _update(*, message_id, user_id, tool_calls, expected_status=None):
        if expected_status is not None and stored_status["value"] != expected_status:
            return False
        stored_status["value"] = tool_calls["status"]
        row["tool_calls"] = tool_calls
        return True

    monkeypatch.setattr(bot_service, "_update_message_tool_calls", _update)

    def _create_booking(*, user_id, payload):
        writes.append({"user_id": user_id, "payload": payload})
        return "booking-1"

    monkeypatch.setattr(bot_service.bookings, "create", _create_booking)
    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: assistant_messages.append(kwargs) or "msg-2",
    )

    first = bot_service.confirm_action(user_id="creator-1", message_id=message_id)
    second = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert first.executed is True
    assert second.executed is False
    assert len(writes) == 1
    assert row["tool_calls"]["status"] == "confirmed"
    assert row["tool_calls"]["result"]["record_id"] == "booking-1"
    assert assistant_messages[-1]["role"] == "assistant"


def test_cancel_writes_nothing(monkeypatch) -> None:
    message_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "submit_creator_listing",
            "payload": {"title": "Collab", "description": "Shoot", "listing_type": "collab"},
            "preview": "Preview",
            "result": None,
        },
    }
    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kwargs: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )

    def _fail_write(**kwargs):
        raise AssertionError("cancel should not write")

    monkeypatch.setattr(bot_service.jobs, "create", _fail_write)
    monkeypatch.setattr(bot_service, "create_message", lambda **kwargs: "msg-2")

    result = bot_service.cancel_action(user_id="creator-1", message_id=message_id)

    assert result.message == "Cancelled. Nothing was saved."
    assert row["tool_calls"]["status"] == "cancelled"


def test_confirm_requires_owner_before_write(monkeypatch) -> None:
    monkeypatch.setattr(
        bot_service,
        "_get_message_for_user",
        lambda *, message_id, user_id: None,
    )

    def _fail_write(**kwargs):
        raise AssertionError("unauthorized confirmation should not write")

    monkeypatch.setattr(bot_service.bookings, "create", _fail_write)

    result = bot_service.confirm_action(
        user_id="creator-2",
        message_id=str(uuid4()),
    )

    assert result.found is False
    assert result.executed is False


def test_unauthorized_user_cannot_confirm_another_users_action(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator", user_id="creator-2")
    monkeypatch.setattr(
        creator_routes.bot,
        "confirm_action",
        lambda *, user_id, message_id: bot_service.BotActionResult(
            message="not found",
            found=False,
        ),
    )

    response = client.post(
        f"/creator/bot/actions/{uuid4()}/confirm",
        follow_redirects=False,
    )

    assert response.status_code == 404


def test_scope_refusal_does_not_call_claude(monkeypatch) -> None:
    created: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})
    monkeypatch.setattr(bot_service, "list_messages", lambda uid, limit=20: [])

    def _fail_claude(**kwargs):
        raise AssertionError("Claude should not be called for out-of-scope requests")

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fail_claude)

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="Can you debug my code?",
    )

    assert result.flagged is True
    assert result.flag_category == "scope"
    assert "creator operations" in result.response
    assert len(created) == 2
    assert created[0]["role"] == "user"
    assert created[0]["flagged"] is True
    assert created[1]["role"] == "assistant"


def test_drafting_request_adds_brand_reply_guidance(monkeypatch) -> None:
    created: list[dict] = []
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: created.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {
                "role": "user",
                "content": "Draft a reply to this brand email about usage rights.",
            }
        ],
    )

    def _fake_claude(**kwargs):
        captured.update(kwargs)
        return bot_service.anthropic_client.ClaudeResponse(
            text="Draft: Thanks for reaching out..."
        )

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="Draft a reply to this brand email about usage rights.",
    )

    system_prompt = str(captured["system_prompt"])
    assert result.response.startswith("Draft:")
    assert "Drafting mode:" in system_prompt
    assert "Kind: brand_reply" in system_prompt
    assert "Do not imply the reply was sent" in system_prompt
    assert "Do not say you posted, sent, booked, updated, or completed anything" in system_prompt
    assert created[-1]["role"] == "assistant"


def test_caption_prompt_sets_babyg_voice_and_profile_tool_policy(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(bot_service, "create_message", lambda **kwargs: "msg-1")
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "Draft captions for my new dinner reel."}
        ],
    )

    def _fake_claude(**kwargs):
        captured.update(kwargs)
        return bot_service.anthropic_client.ClaudeResponse(text="caption options")

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    bot_service.handle_creator_message(
        user_id="creator-1",
        content="Draft captions for my new dinner reel.",
    )

    system_prompt = str(captured["system_prompt"])
    # voice character (high-level manager, not a chatbot)
    assert "high-level personal manager texting a creator" in system_prompt
    # no-hype rule
    assert "no exclamation points" in system_prompt
    assert "no fake hype" in system_prompt
    # tool policy: read_my_profile is required for voice-matched captions
    assert "call read_my_profile before voice-matched captions" in system_prompt
    # caption drafting guidance gets injected when draft_kind == "caption"
    assert "Use read_my_profile before drafting" in system_prompt


def test_drafting_request_adds_creator_dm_guidance(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(bot_service, "create_message", lambda **kwargs: "msg-1")
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "Write a DM to a creator for a collab."}
        ],
    )

    def _fake_claude(**kwargs):
        captured.update(kwargs)
        return bot_service.anthropic_client.ClaudeResponse(text="Draft DM")

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    bot_service.handle_creator_message(
        user_id="creator-1",
        content="Write a DM to a creator for a collab.",
    )

    system_prompt = str(captured["system_prompt"])
    assert "Kind: creator_dm" in system_prompt
    assert "Do not imply it was sent" in system_prompt


def test_hot_drop_request_adds_task_guidance(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(bot_service, "create_message", lambda **kwargs: "msg-1")
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "What hot drops should I act on this week?"}
        ],
    )

    def _fake_claude(**kwargs):
        captured.update(kwargs)
        return bot_service.anthropic_client.ClaudeResponse(text="act on this")

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    bot_service.handle_creator_message(
        user_id="creator-1",
        content="What hot drops should I act on this week?",
    )

    system_prompt = str(captured["system_prompt"])
    assert "Task mode:" in system_prompt
    assert "Kind: hot_drops" in system_prompt
    assert "Call read_intel_feed before answering" in system_prompt


def test_non_drafting_request_uses_base_prompt(monkeypatch) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(bot_service, "create_message", lambda **kwargs: "msg-1")
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})
    monkeypatch.setattr(
        bot_service,
        "list_messages",
        lambda uid, limit=20: [
            {"role": "user", "content": "What should I focus on today?"}
        ],
    )

    def _fake_claude(**kwargs):
        captured.update(kwargs)
        return bot_service.anthropic_client.ClaudeResponse(text="Focus here.")

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    bot_service.handle_creator_message(
        user_id="creator-1",
        content="What should I focus on today?",
    )

    assert "Drafting mode:" not in str(captured["system_prompt"])


def test_build_context_uses_read_only_tool_bundle(monkeypatch) -> None:
    monkeypatch.setattr(
        bot_service.read_only,
        "collect_context",
        lambda user_id: {"read_my_profile": {"name": "Mia"}},
    )

    assert bot_service.build_context("creator-1") == {
        "read_my_profile": {"name": "Mia"}
    }


def test_read_only_context_collects_creator_data(monkeypatch) -> None:
    monkeypatch.setattr(
        read_only.profiles,
        "get_creator_profile",
        lambda uid: {
            "full_name": "Mia Creator",
            "instagram_handle": "miacreates",
            "neighborhood": "Miami",
            "niches": ["food", "wellness"],
            "content_formats": ["reel"],
            "topics": ["new openings"],
            "follower_range": "10k-25k",
            "engagement_range": "3-5%",
            "creator_tenure": "3 years",
            "primary_platform": "instagram",
            "hard_limits": "No gambling.",
            "writing_samples": ["bright caption sample"],
            "tier": "pro",
        },
    )
    monkeypatch.setattr(
        read_only.intel,
        "feed_for_creator",
        lambda *, niches, tier: [
            {
                "title": "New dinner room",
                "category": "venue",
                "confidence": "high",
                "valid_until": "2026-05-30T00:00:00Z",
                "body": "Go early for golden-hour tables.",
            }
        ],
    )
    monkeypatch.setattr(
        read_only.bookings,
        "list_for_user",
        lambda uid, horizon, limit: [
            {
                "starts_at": "2026-05-20T22:00:00Z",
                "title": "Dinner visit",
                "type": "restaurant",
                "status": "confirmed",
                "location": "Miami Beach",
            }
        ],
    )
    monkeypatch.setattr(
        read_only.dms,
        "list_threads_for_user",
        lambda uid: [{"peer_id": "peer-1", "last_message_at": "2026-05-19T12:00:00Z"}],
    )
    monkeypatch.setattr(
        read_only.profiles,
        "get_creators_by_ids",
        lambda ids: {"peer-1": {"full_name": "Ana Peer"}},
    )
    monkeypatch.setattr(
        read_only.receipts,
        "list_for_user",
        lambda uid, limit: [
            {
                "post_type": "reel",
                "posted_at": "2026-05-18",
                "caption_excerpt": "Miami night out",
                "post_url": "https://example.com/reel",
                "like_count": 100,
                "comment_count": 12,
            }
        ],
    )
    monkeypatch.setattr(
        read_only.performance,
        "list_for_user",
        lambda uid, limit: [
            {
                "week_start_date": "2026-05-18",
                "engagement_rate": "4.2",
                "follower_delta": 80,
                "posts_count": 3,
                "active_brand_deals_value": 1200,
            }
        ],
    )
    monkeypatch.setattr(
        read_only.network,
        "list_directory_for_creator",
        lambda uid: [
            {
                "user_id": "peer-2",
                "full_name": "Nia Creator",
                "instagram_handle": "niacreates",
                "neighborhood": "Wynwood",
                "niches": ["style"],
                "follower_range": "25k-50k",
            }
        ],
    )

    context = read_only.collect_context("creator-1")

    assert context["read_my_profile"]["name"] == "Mia Creator"
    assert context["read_my_profile"]["writing_samples"] == ["bright caption sample"]
    assert context["read_intel_feed"][0]["title"] == "New dinner room"
    assert context["read_my_calendar"][0]["title"] == "Dinner visit"
    assert context["read_my_dms"][0]["peer_name"] == "Ana Peer"
    assert context["read_my_receipts"][0]["caption_excerpt"] == "Miami night out"
    assert context["read_my_performance"][0]["active_brand_deals_value"] == 1200
    assert context["read_creator_directory"][0]["name"] == "Nia Creator"


# ---------------------------------------------------------------------------
# Crash protection: a single bad turn should never bubble a 500 to the user.
# ---------------------------------------------------------------------------


def test_bot_persists_friendly_message_when_agent_loop_throws(monkeypatch) -> None:
    """Any unhandled exception inside `_run_agent_loop` must be caught.

    The user's stats-question crash on the live app was a top-level
    exception escaping the bot service. This test pins the safety
    wrapper: tools and the loop can fail in arbitrary ways, and the
    creator still receives a calm assistant message instead of an HTTP
    500. The original user message remains persisted.
    """
    persisted: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: persisted.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(bot_service, "list_messages", lambda uid, limit=20: [])
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})

    def _explode(*_a, **_kw):
        raise RuntimeError("simulated tool serialization failure")

    monkeypatch.setattr(bot_service, "_run_agent_loop", _explode)

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="how did my last post do?",
    )

    # The user message is the first persisted; the assistant fallback is
    # the second. Both must be written, and the assistant text must be
    # the calm fallback, not an empty string or a leaked traceback.
    roles = [str(call.get("role")) for call in persisted]
    assert roles[:2] == ["user", "assistant"]
    assert "snag" in result.response.lower()
    # No fake stats, no invented numbers.
    assert "instagram" not in result.response.lower()
    assert "tiktok" not in result.response.lower()


def test_bot_does_not_crash_on_stats_question_with_empty_data(monkeypatch) -> None:
    """End-to-end mock: stats-style question + empty receipts/performance
    must complete a normal turn (no exception). Claude is mocked so the
    test asserts the integration shape, not the LLM wording."""
    persisted: list[dict[str, object]] = []
    monkeypatch.setattr(
        bot_service,
        "create_message",
        lambda **kwargs: persisted.append(kwargs) or "msg-1",
    )
    monkeypatch.setattr(bot_service, "list_messages", lambda uid, limit=20: [])
    monkeypatch.setattr(bot_service, "build_context", lambda uid: {})

    def _fake_claude(**_kwargs):
        return bot_service.anthropic_client.ClaudeResponse(
            text=(
                "i don't have connected post stats yet. right now i can use "
                "saved performance data, and auto-sync will come after "
                "Meta/TikTok integration."
            )
        )

    monkeypatch.setattr(bot_service.anthropic_client, "complete_chat", _fake_claude)

    result = bot_service.handle_creator_message(
        user_id="creator-1",
        content="what were my last post stats?",
    )

    # Turn completes, response surfaces the stats reality clause.
    assert "saved performance data" in result.response.lower()
    assert "Meta/TikTok integration" in result.response


# ---------------------------------------------------------------------------
# Async / AJAX path: X-Requested-With: fetch returns the bot_messages
# partial instead of redirecting, so the chat surface can swap innerHTML
# without a full-page reload.
# ---------------------------------------------------------------------------


def test_bot_post_ajax_returns_message_partial(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator", user_id="creator-1")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "handle_creator_message",
        lambda *, user_id, content: bot_service.BotTurnResult(response="Drafted."),
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "list_messages",
        lambda uid: [
            {"role": "user", "content": "Draft a caption", "flagged": False},
            {"role": "assistant", "content": "Drafted.", "flagged": False},
        ],
    )

    r = client.post(
        "/creator/bot",
        data={"message": "Draft a caption"},
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    # No redirect — partial returned 200 with HTML body.
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Drafted." in r.text
    assert "Draft a caption" in r.text
    # The partial does NOT include the full page shell — no <html>, no nav.
    assert "<html" not in r.text.lower()
    assert "app-topbar" not in r.text


def test_bot_post_ajax_failure_returns_partial_with_banner(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator", user_id="creator-1")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "handle_creator_message",
        lambda *, user_id, content: bot_service.BotTurnResult(response=""),
    )
    monkeypatch.setattr(creator_routes.bot, "list_messages", lambda uid: [])

    r = client.post(
        "/creator/bot",
        data={"message": "??"},
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    # 400 status, partial body with embedded banner — never a redirect.
    assert r.status_code == 400
    assert r.headers["content-type"].startswith("text/html")
    assert "bot-banner-error" in r.text
    # Jinja HTML-escapes the apostrophe → check for the visible
    # words around it instead of the literal contraction.
    assert "answer that turn" in r.text.lower()


def test_bot_action_confirm_ajax_returns_message_partial(
    monkeypatch, client: TestClient
) -> None:
    message_id = str(uuid4())
    _signed_in(client, role="creator", user_id="creator-1")

    monkeypatch.setattr(
        creator_routes.bot,
        "confirm_action",
        lambda *, user_id, message_id: bot_service.BotActionResult(
            message="confirmed", found=True
        ),
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "list_messages",
        lambda uid: [
            {
                "id": message_id,
                "role": "assistant",
                "content": "Booking saved.",
                "tool_calls": {
                    "kind": "proposed_action",
                    "status": "confirmed",
                    "action_type": "create_booking",
                },
            }
        ],
    )

    r = client.post(
        f"/creator/bot/actions/{message_id}/confirm",
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Booking saved." in r.text
    assert "bot-action-confirmed" in r.text


def test_bot_action_cancel_ajax_returns_message_partial(
    monkeypatch, client: TestClient
) -> None:
    message_id = str(uuid4())
    _signed_in(client, role="creator", user_id="creator-1")

    monkeypatch.setattr(
        creator_routes.bot,
        "cancel_action",
        lambda *, user_id, message_id: bot_service.BotActionResult(
            message="cancelled", found=True
        ),
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "list_messages",
        lambda uid: [
            {
                "id": message_id,
                "role": "assistant",
                "content": "Cancelled. Nothing was saved.",
                "tool_calls": {
                    "kind": "proposed_action",
                    "status": "cancelled",
                    "action_type": "create_booking",
                },
            }
        ],
    )

    r = client.post(
        f"/creator/bot/actions/{message_id}/cancel",
        headers={"X-Requested-With": "fetch"},
        follow_redirects=False,
    )

    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "bot-action-cancelled" in r.text


def test_bot_post_without_ajax_header_still_redirects(
    monkeypatch, client: TestClient
) -> None:
    """No-JS fallback contract: native form post still gets the PRG
    redirect. Any change to this will silently break clients that have
    JS disabled or where bot.js failed to load."""
    _signed_in(client, role="creator", user_id="creator-1")
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.bot,
        "handle_creator_message",
        lambda *, user_id, content: bot_service.BotTurnResult(response="ok"),
    )

    r = client.post(
        "/creator/bot",
        data={"message": "hi"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert r.headers["location"] == "/creator/bot"


# ---------------------------------------------------------------------------
# Phase 1: Tavily web_search tool through the agent loop.
# All assertions are on the tool-execution path inside the bot service.
# Live Tavily calls are mocked at the integration boundary.
# ---------------------------------------------------------------------------


def test_web_search_tool_returns_unavailable_when_key_missing(monkeypatch) -> None:
    """No TAVILY_API_KEY → tool returns available=False with a clear
    reason. The agent loop hands this to Claude as a tool result;
    Claude is prompted (system) to skip search and answer from local
    context. This is the hard-constraint guard for `no fake stats /
    no fake connected states`."""
    monkeypatch.setattr(
        bot_service.tavily, "is_configured", lambda: False
    )
    # If is_configured was bypassed and we hit tavily.search anyway,
    # fail loud so the bug is obvious.
    monkeypatch.setattr(
        bot_service.tavily,
        "search",
        lambda *a, **kw: pytest.fail("tavily.search must not be called when unconfigured"),
    )

    result = bot_service._run_web_search(
        user_id="creator-1",
        tool_input={"query": "anything"},
    )

    assert result["available"] is False
    assert "not configured" in result["reason"].lower()


def test_web_search_tool_returns_hits_when_configured(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.tavily, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.tavily,
        "search",
        lambda q, max_results=5: [
            bot_service.tavily.SearchHit(
                title="venue opening",
                url="https://example.com/a",
                snippet="snippet a",
                published="2026-06-08",
            ),
        ],
    )
    # Reset the counter between tests to keep them order-independent.
    bot_service._web_search_counter.clear()

    result = bot_service._run_web_search(
        user_id="creator-1",
        tool_input={"query": "venue openings this weekend"},
    )

    assert result["available"] is True
    assert result["query"] == "venue openings this weekend"
    assert result["results"][0]["url"] == "https://example.com/a"
    assert result["results"][0]["title"] == "venue opening"


def test_web_search_tool_enforces_daily_cap(monkeypatch) -> None:
    """A runaway model-side loop or chatty creator must not blow
    through Tavily quota. After WEB_SEARCH_DAILY_CAP successful calls
    on the same UTC day, further calls return available=False."""
    monkeypatch.setattr(bot_service.tavily, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.tavily,
        "search",
        lambda q, max_results=5: [],
    )
    bot_service._web_search_counter.clear()

    # Burn the budget.
    for _ in range(bot_service.WEB_SEARCH_DAILY_CAP):
        ok = bot_service._run_web_search(
            user_id="creator-cap",
            tool_input={"query": "x"},
        )
        assert ok["available"] is True

    over = bot_service._run_web_search(
        user_id="creator-cap",
        tool_input={"query": "x"},
    )
    assert over["available"] is False
    assert "daily" in over["reason"].lower()


def test_web_search_tool_handles_upstream_failure(monkeypatch) -> None:
    """A failed Tavily call must not crash the bot turn. The tool
    returns available=False so the agent loop continues and answers
    from local context."""
    monkeypatch.setattr(bot_service.tavily, "is_configured", lambda: True)

    def _boom(query, max_results=5):
        raise bot_service.tavily.TavilyCallError("upstream 503")

    monkeypatch.setattr(bot_service.tavily, "search", _boom)
    bot_service._web_search_counter.clear()

    result = bot_service._run_web_search(
        user_id="creator-1",
        tool_input={"query": "something"},
    )
    assert result["available"] is False
    assert "upstream failed" in result["reason"].lower()


def test_web_search_tool_blank_query_no_op(monkeypatch) -> None:
    """Empty query should never hit Tavily — saves a call against the
    daily cap and against the upstream budget."""
    monkeypatch.setattr(
        bot_service.tavily,
        "search",
        lambda *a, **kw: pytest.fail("blank query reached Tavily"),
    )

    result = bot_service._run_web_search(
        user_id="creator-1",
        tool_input={"query": "   "},
    )
    assert result["available"] is False


def test_web_search_tool_listed_in_bot_tool_definitions() -> None:
    """The tool must be registered so Claude can call it. This pins
    the registration so a future refactor doesn't silently drop it."""
    names = {t["name"] for t in bot_service.prompts.BOT_TOOL_DEFINITIONS}
    assert "web_search" in names


def test_system_prompt_mentions_web_search_policy() -> None:
    """The prompt must explain when to use web_search (current public
    facts) and when NOT to (creator-private data) so Claude doesn't
    burn the daily cap on every turn."""
    p = bot_service.prompts.babyg_system_prompt()
    assert "web_search" in p
    assert "cite" in p.lower()


# ---------------------------------------------------------------------------
# Phase 3 step 4: read_my_instagram_stats bot tool.
# All assertions hit the agent-loop helper. Graph API + token storage
# are mocked at the integration boundary.
# ---------------------------------------------------------------------------


def test_instagram_stats_unavailable_when_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.instagram_meta, "is_configured", lambda: False)
    # Token + graph calls must not run if config check short-circuits.
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_instagram",
        lambda _u: pytest.fail("token lookup should not run when unconfigured"),
    )
    result = bot_service._run_instagram_stats(
        user_id="creator-1", tool_input={"limit": 5}
    )
    assert result["available"] is False
    assert "not configured" in result["reason"]


def test_instagram_stats_unavailable_when_no_connection(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_instagram",
        lambda _u: None,
    )
    bot_service._instagram_stats_counter.clear()
    result = bot_service._run_instagram_stats(
        user_id="creator-1", tool_input={}
    )
    assert result["available"] is False
    # Creator-facing copy should mention how to connect.
    assert "Business or Creator" in result["reason"]


def test_instagram_stats_returns_media_and_insights_when_connected(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_instagram",
        lambda _u: "long-token",
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider": "instagram", "provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "instagram_account_id",
        lambda c: "ig-1",
    )
    monkeypatch.setattr(
        bot_service.instagram_meta,
        "get_user_media",
        lambda token, *, ig_user_id, limit: [
            bot_service.instagram_meta.InstagramMedia(
                media_id="m1",
                caption="dinner at boia",
                media_type="IMAGE",
                permalink="https://www.instagram.com/p/m1",
                timestamp="2026-06-08T18:00:00+0000",
                like_count=120,
                comments_count=4,
            ),
        ],
    )
    monkeypatch.setattr(
        bot_service.instagram_meta,
        "get_media_insights",
        lambda token, *, media_id: {
            "engagement": 50,
            "reach": 900,
            "impressions": None,
            "saved": 6,
        },
    )
    bot_service._instagram_stats_counter.clear()

    result = bot_service._run_instagram_stats(
        user_id="creator-1", tool_input={"limit": 3}
    )
    assert result["available"] is True
    assert len(result["results"]) == 1
    row = result["results"][0]
    assert row["media_id"] == "m1"
    assert row["like_count"] == 120
    assert row["permalink"] == "https://www.instagram.com/p/m1"
    assert row["insights"]["engagement"] == 50
    assert row["insights"]["reach"] == 900
    # Impressions missing → comes back as None, never invented.
    assert row["insights"]["impressions"] is None


def test_instagram_stats_keeps_media_when_insights_fail(monkeypatch) -> None:
    """A bad insights call for one post must not lose the whole list.
    Skip insights for that post, keep the media metadata + like/comment
    counts so the creator still gets something real."""
    monkeypatch.setattr(bot_service.instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_instagram",
        lambda _u: "long-token",
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        bot_service.oauth_connections, "instagram_account_id", lambda c: "ig-1"
    )
    monkeypatch.setattr(
        bot_service.instagram_meta,
        "get_user_media",
        lambda token, *, ig_user_id, limit: [
            bot_service.instagram_meta.InstagramMedia(
                media_id="m1",
                caption=None,
                media_type="REEL",
                permalink=None,
                timestamp=None,
                like_count=10,
                comments_count=1,
            )
        ],
    )

    def _boom(_t, *, media_id):
        raise bot_service.instagram_meta.InstagramError("insights blew up")

    monkeypatch.setattr(bot_service.instagram_meta, "get_media_insights", _boom)
    bot_service._instagram_stats_counter.clear()

    result = bot_service._run_instagram_stats(
        user_id="creator-1", tool_input={}
    )
    assert result["available"] is True
    assert result["results"][0]["like_count"] == 10
    assert result["results"][0]["insights"] == {}


def test_instagram_stats_daily_cap_enforced(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_instagram",
        lambda _u: "long-token",
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        bot_service.oauth_connections, "instagram_account_id", lambda c: "ig-1"
    )
    monkeypatch.setattr(
        bot_service.instagram_meta, "get_user_media", lambda *a, **kw: []
    )
    monkeypatch.setattr(
        bot_service.instagram_meta, "get_media_insights", lambda *a, **kw: {}
    )
    bot_service._instagram_stats_counter.clear()

    for _ in range(bot_service.INSTAGRAM_STATS_DAILY_CAP):
        r = bot_service._run_instagram_stats(
            user_id="creator-cap", tool_input={}
        )
        assert r["available"] is True
    over = bot_service._run_instagram_stats(
        user_id="creator-cap", tool_input={}
    )
    assert over["available"] is False
    assert "daily" in over["reason"].lower()


def test_instagram_stats_handles_graph_failure(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_instagram",
        lambda _u: "long-token",
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        bot_service.oauth_connections, "instagram_account_id", lambda c: "ig-1"
    )

    def _media_fail(*_a, **_kw):
        raise bot_service.instagram_meta.InstagramError("graph 503")

    monkeypatch.setattr(bot_service.instagram_meta, "get_user_media", _media_fail)
    bot_service._instagram_stats_counter.clear()

    result = bot_service._run_instagram_stats(
        user_id="creator-1", tool_input={}
    )
    assert result["available"] is False
    assert "graph api failed" in result["reason"].lower()


def test_instagram_stats_tool_registered_and_prompt_updated() -> None:
    names = {t["name"] for t in bot_service.prompts.BOT_TOOL_DEFINITIONS}
    assert "read_my_instagram_stats" in names

    p = bot_service.prompts.babyg_system_prompt()
    # The stats reality check must mention IG-as-real-when-connected
    # AND keep the exact-sentence fallback for the other platforms.
    assert "read_my_instagram_stats" in p
    assert (
        "i don't have connected post stats yet. right now i can use "
        "saved performance data, and auto-sync will come after "
        "Meta/TikTok integration."
    ) in p
    assert "never invent numbers" in p


# ---------------------------------------------------------------------------
# Slice 1: read_my_gmail bot tool.
# All assertions hit the agent-loop helper. Gmail HTTP + token storage
# are mocked at the integration boundary.
# ---------------------------------------------------------------------------


def test_gmail_unavailable_when_google_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: False)
    # Connection + token lookups must not run if config check short-circuits.
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: pytest.fail("connection lookup should not run when unconfigured"),
    )
    result = bot_service._run_gmail_inbox(
        user_id="creator-1", tool_input={"limit": 5}
    )
    assert result["available"] is False
    assert "not configured" in result["reason"]


def test_gmail_unavailable_when_no_gmail_scope(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    # Connection exists but only has Calendar scope — Gmail should not surface.
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: {"provider": "google", "scopes": [bot_service.google_calendar.CALENDAR_SCOPE]},
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: pytest.fail("token lookup must not run when gmail scope missing"),
    )
    bot_service._gmail_inbox_counter.clear()
    result = bot_service._run_gmail_inbox(
        user_id="creator-1", tool_input={}
    )
    assert result["available"] is False
    assert "isn't connected" in result["reason"]


def test_gmail_returns_threads_when_connected(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: {
            "provider": "google",
            "scopes": [bot_service.google_calendar.GMAIL_READONLY_SCOPE],
        },
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )
    fake_msg = bot_service.google_gmail.GmailMessage(
        message_id="m1",
        thread_id="t1",
        from_="Brand <hi@brand.test>",
        to="creator@example.com",
        subject="re: collab",
        snippet="snippet",
        body_text="body",
        internal_date="2026-06-11T00:00:00+00:00",
        is_unread=True,
    )
    fake_thread = bot_service.google_gmail.GmailThread(
        thread_id="t1",
        snippet="preview",
        messages=[fake_msg],
        is_unread=True,
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "list_recent_threads",
        lambda token, *, limit: [fake_thread],
    )
    bot_service._gmail_inbox_counter.clear()

    result = bot_service._run_gmail_inbox(
        user_id="creator-1", tool_input={"limit": 3}
    )
    assert result["available"] is True
    assert len(result["results"]) == 1
    thread = result["results"][0]
    assert thread["thread_id"] == "t1"
    assert thread["is_unread"] is True
    assert thread["messages"][0]["subject"] == "re: collab"
    assert thread["messages"][0]["body_text"] == "body"


def test_gmail_falls_back_gracefully_on_upstream_error(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: {
            "provider": "google",
            "scopes": [bot_service.google_calendar.GMAIL_READONLY_SCOPE],
        },
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )

    def _boom(*_a, **_kw):
        raise bot_service.google_gmail.GmailError("upstream")

    monkeypatch.setattr(bot_service.google_gmail, "list_recent_threads", _boom)
    bot_service._gmail_inbox_counter.clear()

    result = bot_service._run_gmail_inbox(
        user_id="creator-1", tool_input={}
    )
    assert result["available"] is False
    assert "gmail api failed" in result["reason"].lower()


def test_gmail_unauthorized_signals_reconnect(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: {
            "provider": "google",
            "scopes": [bot_service.google_calendar.GMAIL_READONLY_SCOPE],
        },
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )

    def _boom(*_a, **_kw):
        raise bot_service.google_gmail.GmailUnauthorizedError("expired")

    monkeypatch.setattr(bot_service.google_gmail, "list_recent_threads", _boom)
    bot_service._gmail_inbox_counter.clear()

    result = bot_service._run_gmail_inbox(
        user_id="creator-1", tool_input={}
    )
    assert result["available"] is False
    assert "reconnect" in result["reason"].lower()


def test_gmail_daily_cap_short_circuits(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: {
            "provider": "google",
            "scopes": [bot_service.google_calendar.GMAIL_READONLY_SCOPE],
        },
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "list_recent_threads",
        lambda token, *, limit: [],
    )
    bot_service._gmail_inbox_counter.clear()

    for _ in range(bot_service.GMAIL_INBOX_DAILY_CAP):
        r = bot_service._run_gmail_inbox(
            user_id="creator-cap", tool_input={}
        )
        assert r["available"] is True

    over = bot_service._run_gmail_inbox(
        user_id="creator-cap", tool_input={}
    )
    assert over["available"] is False
    assert "cap reached" in over["reason"]


def test_gmail_tool_registered_and_prompt_updated() -> None:
    names = {t["name"] for t in bot_service.prompts.BOT_TOOL_DEFINITIONS}
    assert "read_my_gmail" in names

    p = bot_service.prompts.babyg_system_prompt()
    assert "read_my_gmail" in p
    assert "inbox reality check" in p
    # The exact phrasing the bot must NOT do is pinned.
    assert "never invent email content" in p


# ---------------------------------------------------------------------------
# Slice 2: create_gmail_draft staged-approval flow.
# Staging never calls Gmail; only confirm does. Cancel and double-confirm
# guarantees come from the existing _update_message_tool_calls CAS.
# ---------------------------------------------------------------------------


def _gmail_compose_connection() -> dict:
    return {
        "provider": "google",
        "scopes": [bot_service.google_calendar.GMAIL_COMPOSE_SCOPE],
    }


def _gmail_send_connection() -> dict:
    return {
        "provider": "google",
        "scopes": [
            bot_service.google_calendar.GMAIL_COMPOSE_SCOPE,
            bot_service.google_calendar.GMAIL_SEND_SCOPE,
        ],
    }


def _calendar_connection() -> dict:
    return {
        "provider": "google",
        "scopes": [bot_service.google_calendar.CALENDAR_SCOPE],
    }


def test_gmail_draft_staging_does_not_create_draft(monkeypatch) -> None:
    """The stage path must NEVER call google_gmail.create_draft. The
    creator has to confirm first; only the confirm path writes."""
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_compose_connection(),
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "create_draft",
        lambda *a, **kw: pytest.fail("staging must not call create_draft"),
    )
    proposal_id = str(uuid4())
    created_proposals: list[dict] = []

    def _create_proposal(**kwargs):
        created_proposals.append(kwargs)
        return {"id": proposal_id, **kwargs}

    monkeypatch.setattr(
        bot_service.action_proposals, "create_proposal", _create_proposal
    )

    result = bot_service._stage_create_gmail_draft_tool(
        {
            "to": "brand@acme.test",
            "subject": "re: collab",
            "body": "hey team — happy to chat thursday.",
        },
        pending_action=None,
        user_id="creator-1",
    )
    assert result["ok"] is True
    assert result["content"]["status"] == "pending_confirmation"
    assert result["content"]["action_type"] == "gmail.create_draft"
    assert result["content"]["proposal_id"] == proposal_id
    assert "never sends" in result["content"]["message"]
    assert created_proposals[0]["action_type"] == "gmail.create_draft"
    preview = created_proposals[0]["preview"]
    assert preview["to"] == "brand@acme.test"
    assert preview["subject"] == "re: collab"
    assert preview["body"].startswith("hey team")
    # Preview must reflect the staged payload — preview is generated
    # from the proposal at stage time.
    assert "brand@acme.test" in result["pending_action"]["preview"]
    assert "re: collab" in result["pending_action"]["preview"]
    assert result["pending_action"]["action_type"] == "gmail.create_draft"
    assert result["pending_action"]["proposal_id"] == proposal_id


def test_gmail_draft_refused_when_compose_scope_missing(monkeypatch) -> None:
    """Legacy gmail.readonly connection must NOT enter pending state.
    The reason copy tells the creator to reconnect."""
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: {
            "provider": "google",
            "scopes": [bot_service.google_calendar.GMAIL_READONLY_SCOPE],
        },
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "create_draft",
        lambda *a, **kw: pytest.fail("must not call create_draft when scope missing"),
    )

    result = bot_service._stage_create_gmail_draft_tool(
        {"to": "a@b.test", "subject": "s", "body": "b"},
        pending_action=None,
        user_id="creator-1",
    )
    assert result["ok"] is False
    assert "reconnect" in result["content"].lower()


def test_gmail_draft_refused_when_not_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        bot_service.google_calendar, "is_configured", lambda: False
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: pytest.fail("must not lookup connection when unconfigured"),
    )
    result = bot_service._stage_create_gmail_draft_tool(
        {"to": "a@b.test", "subject": "s", "body": "b"},
        pending_action=None,
        user_id="creator-1",
    )
    assert result["ok"] is False
    assert "not configured" in result["content"]


def test_gmail_draft_missing_fields_returns_clear_error(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_compose_connection(),
    )
    result = bot_service._stage_create_gmail_draft_tool(
        {"to": "", "subject": "", "body": ""},
        pending_action=None,
        user_id="creator-1",
    )
    assert result["ok"] is False
    assert "Missing required Gmail draft fields" in result["content"]


def test_gmail_draft_confirm_calls_create_once_and_double_confirm_is_idempotent(
    monkeypatch,
) -> None:
    """Pinned by the spec: 'confirm creates exactly one draft' and
    'double confirm does not duplicate'. Idempotency is enforced by
    the compare-and-swap on expected_status='pending'."""
    message_id = str(uuid4())
    proposal_id = str(uuid4())
    tool_calls = {
        "kind": "proposed_action",
        "status": "pending",
        "action_type": "gmail.create_draft",
        "proposal_id": proposal_id,
        "payload": {
            "to": "brand@acme.test",
            "subject": "re: collab",
            "body": "draft body",
        },
        "preview": "Preview",
        "result": None,
    }
    row = {"id": message_id, "user_id": "creator-1", "tool_calls": tool_calls}
    stored_status = {"value": "pending"}
    proposal_status = {"value": "pending"}
    draft_calls: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "_get_message_for_user",
        lambda *, message_id, user_id: {
            **row,
            "tool_calls": {**row["tool_calls"], "status": stored_status["value"]},
        }
        if user_id == "creator-1"
        else None,
    )

    def _update(*, message_id, user_id, tool_calls, expected_status=None):
        if expected_status is not None and stored_status["value"] != expected_status:
            return False
        stored_status["value"] = tool_calls["status"]
        row["tool_calls"] = tool_calls
        return True

    monkeypatch.setattr(bot_service, "_update_message_tool_calls", _update)
    monkeypatch.setattr(
        bot_service.action_proposals,
        "confirm_proposal",
        lambda *, proposal_id, user_id: proposal_status["value"] == "pending"
        and not proposal_status.update({"value": "confirmed"}),
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "mark_executing",
        lambda *, proposal_id, user_id: proposal_status["value"] == "confirmed"
        and not proposal_status.update({"value": "executing"}),
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "mark_executed",
        lambda *, proposal_id, user_id, external_result_id=None: proposal_status[
            "value"
        ]
        == "executing"
        and not proposal_status.update({"value": "executed"}),
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_compose_connection(),
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )

    def _create(token, *, to, subject, body, thread_id=None):
        draft_calls.append(
            {"token": token, "to": to, "subject": subject, "body": body}
        )
        return f"draft-{len(draft_calls)}"

    monkeypatch.setattr(bot_service.google_gmail, "create_draft", _create)
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    first = bot_service.confirm_action(user_id="creator-1", message_id=message_id)
    second = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert first.executed is True
    assert first.record_id == "draft-1"
    assert "Gmail draft saved" in first.message
    assert "babyg does not send" in first.message
    # Second confirm finds non-pending status → never re-invokes create_draft.
    assert second.executed is False
    assert len(draft_calls) == 1
    assert row["tool_calls"]["status"] == "executed"
    assert proposal_status["value"] == "executed"


def test_gmail_draft_confirm_missing_scope_blocks_create(monkeypatch) -> None:
    message_id = str(uuid4())
    proposal_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "gmail.create_draft",
            "proposal_id": proposal_id,
            "payload": {
                "to": "brand@acme.test",
                "subject": "re: collab",
                "body": "draft body",
            },
            "preview": "Preview",
            "result": None,
        },
    }

    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kw: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "confirm_proposal",
        lambda *, proposal_id, user_id: False,
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "get_for_user",
        lambda *, proposal_id, user_id: {
            "id": proposal_id,
            "status": "failed",
            "error_code": "missing_required_scope",
        },
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "create_draft",
        lambda *a, **kw: pytest.fail("missing scope must block create_draft"),
    )
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    result = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert result.executed is False
    assert result.action_type == "gmail.create_draft"
    assert row["tool_calls"]["status"] == "failed"
    assert row["tool_calls"]["result"]["error"] == "missing_required_scope"


def test_gmail_draft_cancel_does_not_create_draft(monkeypatch) -> None:
    """Cancel must skip the Gmail call entirely."""
    message_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "gmail.create_draft",
            "proposal_id": str(uuid4()),
            "payload": {
                "to": "x@y.test",
                "subject": "s",
                "body": "b",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kw: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )

    def _no_create(*a, **kw):
        raise AssertionError("cancel must not call create_draft")

    monkeypatch.setattr(bot_service.google_gmail, "create_draft", _no_create)
    cancelled: list[str] = []
    monkeypatch.setattr(
        bot_service.action_proposals,
        "cancel_proposal",
        lambda *, proposal_id, user_id: cancelled.append(proposal_id) or True,
    )
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    result = bot_service.cancel_action(
        user_id="creator-1", message_id=message_id
    )
    assert result.action_type == "gmail.create_draft"
    assert "Cancelled" in result.message
    assert cancelled == [row["tool_calls"]["proposal_id"]]


def test_gmail_draft_tool_registered_and_prompt_updated() -> None:
    names = {t["name"] for t in bot_service.prompts.BOT_TOOL_DEFINITIONS}
    assert "create_gmail_draft" in names
    assert "create_gmail_draft" in bot_service.ALLOWED_ACTION_TYPES
    assert "gmail.create_draft" in bot_service.ALLOWED_ACTION_TYPES

    p = bot_service.prompts.babyg_system_prompt()
    assert "create_gmail_draft" in p
    # The hard guarantee must appear in the prompt.
    assert "babyg never sends" in p or "never sends" in p


def test_gmail_send_staging_does_not_send(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_send_connection(),
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "send_message",
        lambda *a, **kw: pytest.fail("staging must not call send_message"),
    )
    proposal_id = str(uuid4())
    created_proposals: list[dict] = []

    def _create_proposal(**kwargs):
        created_proposals.append(kwargs)
        return {"id": proposal_id, **kwargs}

    monkeypatch.setattr(
        bot_service.action_proposals, "create_proposal", _create_proposal
    )

    result = bot_service._stage_send_gmail_email_tool(
        {
            "to": "brand@acme.test",
            "subject": "approved reply",
            "body": "hi team - approved to send.",
        },
        pending_action=None,
        user_id="creator-1",
    )

    assert result["ok"] is True
    assert result["content"]["status"] == "pending_confirmation"
    assert result["content"]["action_type"] == "gmail.send_email"
    assert result["content"]["proposal_id"] == proposal_id
    assert created_proposals[0]["action_type"] == "gmail.send_email"
    preview = created_proposals[0]["preview"]
    assert preview["to"] == "brand@acme.test"
    assert preview["subject"] == "approved reply"
    assert preview["body"].startswith("hi team")
    assert result["pending_action"]["action_type"] == "gmail.send_email"


def test_gmail_send_refused_when_send_scope_missing(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_compose_connection(),
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "send_message",
        lambda *a, **kw: pytest.fail("must not send when scope missing"),
    )

    result = bot_service._stage_send_gmail_email_tool(
        {"to": "a@b.test", "subject": "s", "body": "b"},
        pending_action=None,
        user_id="creator-1",
    )

    assert result["ok"] is False
    assert "reconnect" in result["content"].lower()


def test_gmail_send_confirm_calls_send_once_and_double_confirm_is_idempotent(
    monkeypatch,
) -> None:
    message_id = str(uuid4())
    proposal_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "gmail.send_email",
            "proposal_id": proposal_id,
            "payload": {
                "to": "brand@acme.test",
                "subject": "approved reply",
                "body": "send body",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    stored_status = {"value": "pending"}
    proposal_status = {"value": "pending"}
    send_calls: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "_get_message_for_user",
        lambda *, message_id, user_id: {
            **row,
            "tool_calls": {**row["tool_calls"], "status": stored_status["value"]},
        }
        if user_id == "creator-1"
        else None,
    )

    def _update(*, message_id, user_id, tool_calls, expected_status=None):
        if expected_status is not None and stored_status["value"] != expected_status:
            return False
        stored_status["value"] = tool_calls["status"]
        row["tool_calls"] = tool_calls
        return True

    monkeypatch.setattr(bot_service, "_update_message_tool_calls", _update)
    monkeypatch.setattr(
        bot_service.action_proposals,
        "confirm_proposal",
        lambda *, proposal_id, user_id: proposal_status["value"] == "pending"
        and not proposal_status.update({"value": "confirmed"}),
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "mark_executing",
        lambda *, proposal_id, user_id: proposal_status["value"] == "confirmed"
        and not proposal_status.update({"value": "executing"}),
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "mark_executed",
        lambda *, proposal_id, user_id, external_result_id=None: proposal_status[
            "value"
        ]
        == "executing"
        and not proposal_status.update({"value": "executed"}),
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_send_connection(),
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )

    def _send(token, *, to, subject, body, thread_id=None):
        send_calls.append(
            {"token": token, "to": to, "subject": subject, "body": body}
        )
        return f"msg-{len(send_calls)}"

    monkeypatch.setattr(bot_service.google_gmail, "send_message", _send)
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    first = bot_service.confirm_action(user_id="creator-1", message_id=message_id)
    second = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert first.executed is True
    assert first.record_id == "msg-1"
    assert "Email sent" in first.message
    assert second.executed is False
    assert len(send_calls) == 1
    assert row["tool_calls"]["status"] == "executed"
    assert proposal_status["value"] == "executed"


def test_gmail_send_confirm_missing_scope_blocks_send(monkeypatch) -> None:
    message_id = str(uuid4())
    proposal_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "gmail.send_email",
            "proposal_id": proposal_id,
            "payload": {
                "to": "brand@acme.test",
                "subject": "approved reply",
                "body": "send body",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kw: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "confirm_proposal",
        lambda *, proposal_id, user_id: False,
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "get_for_user",
        lambda *, proposal_id, user_id: {
            "id": proposal_id,
            "status": "failed",
            "error_code": "missing_required_scope",
        },
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "send_message",
        lambda *a, **kw: pytest.fail("missing scope must block send_message"),
    )
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    result = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert result.executed is False
    assert result.action_type == "gmail.send_email"
    assert row["tool_calls"]["status"] == "failed"
    assert row["tool_calls"]["result"]["error"] == "missing_required_scope"


def test_gmail_send_cancel_does_not_send(monkeypatch) -> None:
    message_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "gmail.send_email",
            "proposal_id": str(uuid4()),
            "payload": {
                "to": "x@y.test",
                "subject": "s",
                "body": "b",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kw: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )
    monkeypatch.setattr(
        bot_service.google_gmail,
        "send_message",
        lambda *a, **kw: pytest.fail("cancel must not call send_message"),
    )
    cancelled: list[str] = []
    monkeypatch.setattr(
        bot_service.action_proposals,
        "cancel_proposal",
        lambda *, proposal_id, user_id: cancelled.append(proposal_id) or True,
    )
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    result = bot_service.cancel_action(user_id="creator-1", message_id=message_id)

    assert result.action_type == "gmail.send_email"
    assert "Cancelled" in result.message
    assert cancelled == [row["tool_calls"]["proposal_id"]]


def test_gmail_send_tool_registered_and_prompt_updated() -> None:
    names = {t["name"] for t in bot_service.prompts.BOT_TOOL_DEFINITIONS}
    assert "send_gmail_email" in names
    assert "gmail.send_email" in bot_service.ALLOWED_ACTION_TYPES

    p = bot_service.prompts.babyg_system_prompt()
    assert "send_gmail_email" in p
    assert "creator must click confirm" in p


def test_calendar_event_staging_does_not_create_event(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _calendar_connection(),
    )
    monkeypatch.setattr(
        bot_service.google_calendar,
        "create_primary_event",
        lambda *a, **kw: pytest.fail("staging must not call create_primary_event"),
    )
    proposal_id = str(uuid4())
    created_proposals: list[dict] = []

    def _create_proposal(**kwargs):
        created_proposals.append(kwargs)
        return {"id": proposal_id, **kwargs}

    monkeypatch.setattr(
        bot_service.action_proposals, "create_proposal", _create_proposal
    )

    result = bot_service._stage_google_calendar_event_tool(
        {
            "title": "Brand call",
            "starts_at": "2099-05-08T10:00:00Z",
            "ends_at": "2099-05-08T10:30:00Z",
            "location": "Zoom",
            "notes": "Pitch the capsule.",
        },
        pending_action=None,
        user_id="creator-1",
    )

    assert result["ok"] is True
    assert result["content"]["status"] == "pending_confirmation"
    assert result["content"]["action_type"] == "calendar.create_event"
    assert result["content"]["proposal_id"] == proposal_id
    assert created_proposals[0]["action_type"] == "calendar.create_event"
    preview = created_proposals[0]["preview"]
    assert preview["title"] == "Brand call"
    assert preview["starts_at"] == "2099-05-08T10:00:00Z"
    assert preview["location"] == "Zoom"
    assert result["pending_action"]["action_type"] == "calendar.create_event"


def test_calendar_event_refused_when_calendar_scope_missing(monkeypatch) -> None:
    monkeypatch.setattr(bot_service.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _gmail_send_connection(),
    )
    monkeypatch.setattr(
        bot_service.google_calendar,
        "create_primary_event",
        lambda *a, **kw: pytest.fail("must not create when scope missing"),
    )

    result = bot_service._stage_google_calendar_event_tool(
        {"title": "Brand call", "starts_at": "2099-05-08T10:00:00Z"},
        pending_action=None,
        user_id="creator-1",
    )

    assert result["ok"] is False
    assert "connect Calendar" in result["content"]


def test_calendar_event_confirm_calls_create_once_and_double_confirm_is_idempotent(
    monkeypatch,
) -> None:
    message_id = str(uuid4())
    proposal_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "calendar.create_event",
            "proposal_id": proposal_id,
            "payload": {
                "title": "Brand call",
                "starts_at": "2099-05-08T10:00:00Z",
                "ends_at": "2099-05-08T10:30:00Z",
                "location": "Zoom",
                "notes": "Pitch.",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    stored_status = {"value": "pending"}
    proposal_status = {"value": "pending"}
    create_calls: list[dict] = []

    monkeypatch.setattr(
        bot_service,
        "_get_message_for_user",
        lambda *, message_id, user_id: {
            **row,
            "tool_calls": {**row["tool_calls"], "status": stored_status["value"]},
        }
        if user_id == "creator-1"
        else None,
    )

    def _update(*, message_id, user_id, tool_calls, expected_status=None):
        if expected_status is not None and stored_status["value"] != expected_status:
            return False
        stored_status["value"] = tool_calls["status"]
        row["tool_calls"] = tool_calls
        return True

    monkeypatch.setattr(bot_service, "_update_message_tool_calls", _update)
    monkeypatch.setattr(
        bot_service.action_proposals,
        "confirm_proposal",
        lambda *, proposal_id, user_id: proposal_status["value"] == "pending"
        and not proposal_status.update({"value": "confirmed"}),
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "mark_executing",
        lambda *, proposal_id, user_id: proposal_status["value"] == "confirmed"
        and not proposal_status.update({"value": "executing"}),
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "mark_executed",
        lambda *, proposal_id, user_id, external_result_id=None: proposal_status[
            "value"
        ]
        == "executing"
        and not proposal_status.update({"value": "executed"}),
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "get_google_connection",
        lambda _u: _calendar_connection(),
    )
    monkeypatch.setattr(
        bot_service.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )

    def _create(token, *, title, starts_at, ends_at=None, notes=None, location=None):
        create_calls.append(
            {
                "token": token,
                "title": title,
                "starts_at": starts_at,
                "ends_at": ends_at,
                "notes": notes,
                "location": location,
            }
        )
        return f"gcal-{len(create_calls)}"

    monkeypatch.setattr(bot_service.google_calendar, "create_primary_event", _create)
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    first = bot_service.confirm_action(user_id="creator-1", message_id=message_id)
    second = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert first.executed is True
    assert first.record_id == "gcal-1"
    assert "Google Calendar event created" in first.message
    assert second.executed is False
    assert len(create_calls) == 1
    assert create_calls[0]["title"] == "Brand call"
    assert row["tool_calls"]["status"] == "executed"
    assert proposal_status["value"] == "executed"


def test_calendar_event_confirm_missing_scope_blocks_create(monkeypatch) -> None:
    message_id = str(uuid4())
    proposal_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "calendar.create_event",
            "proposal_id": proposal_id,
            "payload": {
                "title": "Brand call",
                "starts_at": "2099-05-08T10:00:00Z",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kw: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "confirm_proposal",
        lambda *, proposal_id, user_id: False,
    )
    monkeypatch.setattr(
        bot_service.action_proposals,
        "get_for_user",
        lambda *, proposal_id, user_id: {
            "id": proposal_id,
            "status": "failed",
            "error_code": "missing_required_scope",
        },
    )
    monkeypatch.setattr(
        bot_service.google_calendar,
        "create_primary_event",
        lambda *a, **kw: pytest.fail("missing scope must block event create"),
    )
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    result = bot_service.confirm_action(user_id="creator-1", message_id=message_id)

    assert result.executed is False
    assert result.action_type == "calendar.create_event"
    assert row["tool_calls"]["status"] == "failed"
    assert row["tool_calls"]["result"]["error"] == "missing_required_scope"


def test_calendar_event_cancel_does_not_create(monkeypatch) -> None:
    message_id = str(uuid4())
    row = {
        "id": message_id,
        "user_id": "creator-1",
        "tool_calls": {
            "kind": "proposed_action",
            "status": "pending",
            "action_type": "calendar.create_event",
            "proposal_id": str(uuid4()),
            "payload": {
                "title": "Brand call",
                "starts_at": "2099-05-08T10:00:00Z",
            },
            "preview": "Preview",
            "result": None,
        },
    }
    monkeypatch.setattr(bot_service, "_get_message_for_user", lambda **kw: row)
    monkeypatch.setattr(
        bot_service,
        "_update_message_tool_calls",
        lambda *, message_id, user_id, tool_calls, expected_status=None: row.update(
            {"tool_calls": tool_calls}
        )
        or True,
    )
    monkeypatch.setattr(
        bot_service.google_calendar,
        "create_primary_event",
        lambda *a, **kw: pytest.fail("cancel must not create event"),
    )
    cancelled: list[str] = []
    monkeypatch.setattr(
        bot_service.action_proposals,
        "cancel_proposal",
        lambda *, proposal_id, user_id: cancelled.append(proposal_id) or True,
    )
    monkeypatch.setattr(bot_service, "create_message", lambda **_kw: "msg-2")

    result = bot_service.cancel_action(user_id="creator-1", message_id=message_id)

    assert result.action_type == "calendar.create_event"
    assert "Cancelled" in result.message
    assert cancelled == [row["tool_calls"]["proposal_id"]]


def test_calendar_event_tool_registered_and_prompt_updated() -> None:
    names = {t["name"] for t in bot_service.prompts.BOT_TOOL_DEFINITIONS}
    assert "create_google_calendar_event" in names
    assert "calendar.create_event" in bot_service.ALLOWED_ACTION_TYPES

    p = bot_service.prompts.babyg_system_prompt()
    assert "create_google_calendar_event" in p
    assert "creator must click confirm" in p
