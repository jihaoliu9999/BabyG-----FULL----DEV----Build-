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
        "location_city": "Los Angeles",
        "location_region": "California",
        "location_country": "United States",
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
    assert 'action="/creator/profile/location"' in response.text
    assert "Los Angeles, California" in response.text
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


def test_creator_profile_location_update_saves_manual_location(
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
        "/creator/profile/location",
        data={
            "location_city": "  New   York  ",
            "location_region": "New York",
            "location_country": "United States",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/profile?details=ok"
    assert saved == {
        "uid": "creator-1",
        "payload": {
            "location_city": "New York",
            "location_region": "New York",
            "location_country": "United States",
            "location_lat": None,
            "location_lng": None,
            "location_source": "manual",
            "location_updated_at": saved["payload"]["location_updated_at"],
        },
    }


def test_creator_profile_location_rejects_invalid_lat_lng(
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
        "/creator/profile/location",
        data={
            "location_city": "Los Angeles",
            "location_source": "browser",
            "location_lat": "34.0522",
            "location_lng": "-181",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/profile?details=invalid_location"
    assert saved == {}


def test_creator_profile_location_saves_browser_coords_without_city(
    monkeypatch, client: TestClient
) -> None:
    """The "use my location" flow should save successfully even when
    the user didn't type a city. Public-projection privacy is
    unchanged — coords without a label show no public location text,
    but the row stores the lat/lng for proximity features."""
    _signed_in(client, role="creator", user_id="creator-1")
    saved: dict = {}

    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.profiles,
        "update_creator_profile",
        lambda uid, payload: saved.update(payload) or True,
    )

    response = client.post(
        "/creator/profile/location",
        data={
            "location_city": "",
            "location_region": "",
            "location_country": "",
            "location_source": "browser",
            "location_lat": "34.0522",
            "location_lng": "-118.2437",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/creator/profile?details=ok"
    # Coords saved, source preserved as "browser" since coords are real.
    assert saved["location_lat"] == 34.0522
    assert saved["location_lng"] == -118.2437
    assert saved["location_source"] == "browser"
    # No public label fields — they weren't supplied.
    assert saved["location_city"] is None
    assert saved["location_region"] is None
    assert saved["location_country"] is None


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


# ---------------------------------------------------------------------------
# Operational diagnostic: /creator/_debug/integrations
# Exposes only booleans + resolved redirect URIs — never tokens / IDs /
# secrets. Used to verify Railway env vars are actually being read by
# the running process when the integrations grid looks stale.
# ---------------------------------------------------------------------------


def test_integrations_debug_requires_creator(client: TestClient) -> None:
    _signed_in(client, role="operator")
    r = client.get("/creator/_debug/integrations")
    assert r.status_code == 403


def test_integrations_debug_returns_booleans_and_redirects(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, role="creator")
    monkeypatch.setattr(creator_routes.google_calendar, "is_configured", lambda: True)
    monkeypatch.setattr(
        creator_routes.google_calendar,
        "redirect_uri",
        lambda: "https://example.test/creator/google/calendar/callback",
    )
    monkeypatch.setattr(creator_routes.instagram_meta, "is_configured", lambda: False)
    monkeypatch.setattr(
        creator_routes.instagram_meta,
        "redirect_uri",
        lambda: "https://example.test/creator/instagram/callback",
    )

    r = client.get("/creator/_debug/integrations")
    assert r.status_code == 200
    data = r.json()
    assert data["google"]["configured"] is True
    assert data["google"]["redirect_uri_sent_to_provider"].endswith(
        "/creator/google/calendar/callback"
    )
    assert data["instagram"]["configured"] is False
    assert data["instagram"]["redirect_uri_sent_to_provider"].endswith(
        "/creator/instagram/callback"
    )
    # Never leaks secrets.
    raw = r.text
    for forbidden in ("client_secret", "app_secret", "access_token", "refresh_token"):
        assert forbidden not in raw


def test_integrations_debug_survives_integration_module_errors(
    monkeypatch, client: TestClient
) -> None:
    """Defensive: even if a future integration helper raises during the
    redirect-resolution call, the diagnostic must still return 200 so
    operators can see the configured booleans."""
    _signed_in(client, role="creator")

    def _boom():
        raise RuntimeError("config broken")

    monkeypatch.setattr(creator_routes.google_calendar, "is_configured", _boom)
    monkeypatch.setattr(creator_routes.google_calendar, "redirect_uri", _boom)
    monkeypatch.setattr(creator_routes.instagram_meta, "is_configured", _boom)
    monkeypatch.setattr(creator_routes.instagram_meta, "redirect_uri", _boom)

    r = client.get("/creator/_debug/integrations")
    assert r.status_code == 200
    data = r.json()
    assert data["google"]["configured"] is False
    assert data["google"]["redirect_uri_sent_to_provider"] is None
    assert data["instagram"]["configured"] is False
    assert data["instagram"]["redirect_uri_sent_to_provider"] is None
