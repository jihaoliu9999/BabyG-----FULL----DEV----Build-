"""Phase 2 babyg assistant scaffold tests."""

from __future__ import annotations

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
    assert "What are we making happen?" in response.text
    assert "Need a caption" in response.text
    assert "Drafting it." in response.text


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
