"""Phase 2 babyg assistant scaffold tests."""

from __future__ import annotations

from uuid import uuid4

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
    assert "what needs handling?" in response.text
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
