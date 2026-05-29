"""Creator profile/settings page tests."""

from __future__ import annotations

from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
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
        "niches": ["food", "style"],
        "hard_limits": ["gambling"],
        "tier": "pro",
        "writing_samples": ["sample"],
    }


def test_creator_profile_page_renders(monkeypatch, client: TestClient) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(creator_routes.profiles, "get_creator_profile", lambda uid: _profile())

    response = client.get("/creator/profile")

    assert response.status_code == 200
    assert "Mia Creator" in response.text
    # Profile photo upload + niche editing are not yet wired to the
    # backend (no Storage bucket, no PATCH endpoint). The page surfaces
    # this honestly instead of pretending the upload works.
    assert "coming soon" in response.text
    assert "/auth/logout" in response.text
    assert "/creator/profile/settings" in response.text


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
