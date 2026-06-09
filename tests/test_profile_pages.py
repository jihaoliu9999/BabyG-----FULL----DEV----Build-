"""Creator profile/settings page tests."""

from __future__ import annotations

from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.integrations import google_calendar
from app.routes import creator as creator_routes


def _signed_in(client: TestClient, *, role: str, user_id: str = "creator-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _profile() -> dict:
    return {
        "onboarding_completed_at": "2026-05-01T00:00:00Z",
        "full_name": "Mia Creator",
        "instagram_handle": "miacreates",
        "neighborhood": "miami",
        "primary_platform": "instagram",
        "follower_range": "10k-25k",
        "engagement_range": "3-5%",
        "niches": ["food", "style", ""],
        "content_formats": ["reels", "stories", " "],
        "hard_limits": ["no gambling", ""],
        "bio": "lifestyle creator - party videos",
        "tier": "pro",
        "writing_samples": ["sample"],
    }


def test_creator_profile_page_renders(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())

    response = client.get("/creator/profile")

    assert response.status_code == 200
    assert "Mia Creator" in response.text
    assert "edit niches" in response.text
    assert "edit formats" in response.text
    assert "edit limits" in response.text
    assert 'data-profile-chip-open="niches"' in response.text
    assert 'value="lifestyle"' in response.text
    assert 'value="no alcohol"' in response.text
    assert 'class="chip profile-chip-static"></span>' not in response.text
    assert "edit bio" in response.text
    assert "what should babyg know about how you show up?" in response.text
    assert 'action="/creator/profile/neighborhood"' in response.text
    assert "creator_tenure" not in response.text
    assert "tenure" not in response.text
    assert "/auth/logout" in response.text
    assert "/creator/profile/settings" in response.text


def test_creator_profile_chip_update_saves_existing_fields(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator")
    saved: dict = {}

    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())
    monkeypatch.setattr(
        creator_routes.profiles,
        "update_creator_profile",
        lambda uid, payload: saved.setdefault("payload", payload) or True,
    )

    response = client.post(
        "/creator/profile/chips",
        data={
            "section": "limits",
            "values": ["no alcohol", "no gambling", "", "<script>"],
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/profile?chips=ok"
    assert saved["payload"] == {"hard_limits": ["no alcohol", "no gambling"]}


def test_creator_profile_bio_update_saves_own_profile(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator", user_id="creator-1")
    saved: dict = {}

    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())
    monkeypatch.setattr(
        creator_routes.profiles,
        "update_creator_profile",
        lambda uid, payload: saved.update({"uid": uid, "payload": payload}) or True,
    )

    response = client.post(
        "/creator/profile/bio",
        data={"bio": "  lifestyle creator\n  party videos  "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/profile?bio=ok"
    assert saved == {
        "uid": "creator-1",
        "payload": {"bio": "lifestyle creator\nparty videos"},
    }


def test_creator_profile_bio_update_clears_blank_bio(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator", user_id="creator-1")
    saved: dict = {}

    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())
    monkeypatch.setattr(
        creator_routes.profiles,
        "update_creator_profile",
        lambda uid, payload: saved.update(payload) or True,
    )

    response = client.post(
        "/creator/profile/bio",
        data={"bio": "   "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert saved == {"bio": None}


def test_creator_profile_neighborhood_update_saves_existing_field(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator", user_id="creator-1")
    saved: dict = {}

    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())
    monkeypatch.setattr(
        creator_routes.profiles,
        "update_creator_profile",
        lambda uid, payload: saved.update({"uid": uid, "payload": payload}) or True,
    )

    response = client.post(
        "/creator/profile/neighborhood",
        data={"neighborhood": "  Coral   Gables  "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/profile?details=ok"
    assert saved == {
        "uid": "creator-1",
        "payload": {"neighborhood": "Coral Gables"},
    }


def test_creator_profile_settings_page_renders(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(creator_routes.google_calendar, "is_configured", lambda: False)

    response = client.get("/creator/profile/settings")

    assert response.status_code == 200
    assert "account" in response.text
    assert "not configured" in response.text


def test_creator_profile_settings_google_states_are_scope_aware(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: {"scopes": [google_calendar.CALENDAR_SCOPE]},
    )
    monkeypatch.setattr(creator_routes.google_calendar, "is_configured", lambda: True)

    response = client.get("/creator/profile/settings")

    assert response.status_code == 200
    assert "disconnect Calendar" in response.text
    assert "connect Gmail" in response.text
    assert "href=\"/creator/google/connect?service=gmail&next=/creator/profile/settings\"" in response.text
    assert "/creator/gmail/connect" not in response.text
