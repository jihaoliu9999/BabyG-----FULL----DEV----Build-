"""Instagram OAuth routes.

Covers the three new routes (`/creator/instagram/connect`,
`/creator/instagram/callback`, `/creator/instagram/disconnect`) and
the integrations-grid partial flip from coming-soon to real connect
when the integration is configured.

Heavy mocking at the integration boundary — no live Graph calls.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.security import SESSION_COOKIE, write_session
from app.integrations import instagram_meta
from app.routes import creator as creator_routes


def _signed_in(
    client: TestClient, *, role: str = "creator", user_id: str = "creator-1"
) -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


@pytest.fixture(autouse=True)
def _reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# /creator/instagram/connect
# ---------------------------------------------------------------------------


def test_connect_requires_creator(client: TestClient) -> None:
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/instagram/connect", follow_redirects=False)
    assert r.status_code == 403


def test_connect_redirects_to_settings_when_not_configured(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    r = client.get("/creator/instagram/connect", follow_redirects=False)
    assert r.status_code == 303
    assert "instagram=not_configured" in r.headers["location"]


def test_connect_redirects_to_facebook_oauth_when_configured(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        instagram_meta,
        "auth_url",
        lambda state: f"https://www.facebook.com/v19.0/dialog/oauth?state={state}",
    )
    r = client.get("/creator/instagram/connect", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith(
        "https://www.facebook.com/v19.0/dialog/oauth"
    )


# ---------------------------------------------------------------------------
# /creator/instagram/callback
# ---------------------------------------------------------------------------


def _stub_verify_state(monkeypatch, user_id: str = "creator-1", next_path: str = "/creator/profile/settings"):
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "verify_instagram_state",
        lambda _s: {"user_id": user_id, "next": next_path},
    )


def test_callback_redirects_denied_when_user_cancels(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    r = client.get(
        "/creator/instagram/callback?error=access_denied",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].endswith("/creator/profile/settings?instagram=denied")


def test_callback_redirects_bad_when_state_or_code_missing(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "verify_instagram_state",
        lambda _s: None,
    )
    r = client.get(
        "/creator/instagram/callback?code=abc&state=garbage",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "instagram=bad_callback" in r.headers["location"]


def test_callback_refuses_when_state_user_id_differs(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client, user_id="creator-1")
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "verify_instagram_state",
        lambda _s: {"user_id": "different-user", "next": "/"},
    )
    r = client.get(
        "/creator/instagram/callback?code=abc&state=ok",
        follow_redirects=False,
    )
    assert r.status_code == 403


def test_callback_handles_ineligible_account(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    _stub_verify_state(monkeypatch)
    monkeypatch.setattr(
        instagram_meta,
        "exchange_code",
        lambda _c: {"access_token": "tok", "expires_in": 5184000},
    )

    def _ineligible(_t):
        raise instagram_meta.InstagramIneligibleAccountError(
            "Instagram connection requires an Instagram Business or "
            "Creator account linked to a Facebook Page."
        )

    monkeypatch.setattr(instagram_meta, "resolve_business_account", _ineligible)
    # Save MUST NOT fire when the account is ineligible.
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "save_instagram_connection",
        lambda *a, **kw: pytest.fail("save must not run for ineligible accounts"),
    )

    r = client.get(
        "/creator/instagram/callback?code=abc&state=ok",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "instagram=ineligible" in r.headers["location"]


def test_callback_handles_exchange_failure(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    _stub_verify_state(monkeypatch)

    def _boom(_c):
        raise instagram_meta.InstagramError("token exchange failed")

    monkeypatch.setattr(instagram_meta, "exchange_code", _boom)
    r = client.get(
        "/creator/instagram/callback?code=abc&state=ok",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "instagram=exchange_failed" in r.headers["location"]


def test_callback_happy_path_saves_and_redirects(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    _stub_verify_state(monkeypatch, next_path="/creator/profile/settings")

    monkeypatch.setattr(
        instagram_meta,
        "exchange_code",
        lambda _c: {"access_token": "tok", "expires_in": 5184000},
    )
    monkeypatch.setattr(
        instagram_meta,
        "resolve_business_account",
        lambda _t: instagram_meta.InstagramAccount(
            ig_user_id="ig-1", username="miacreates", name="Mia"
        ),
    )

    saved: dict[str, Any] = {}

    def _save(user_id, token_response, *, ig_account):
        saved["user_id"] = user_id
        saved["token"] = token_response["access_token"]
        saved["ig_user_id"] = ig_account.ig_user_id
        return True

    monkeypatch.setattr(
        creator_routes.oauth_connections, "save_instagram_connection", _save
    )

    r = client.get(
        "/creator/instagram/callback?code=abc&state=ok",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "instagram=connected" in r.headers["location"]
    assert saved == {
        "user_id": "creator-1",
        "token": "tok",
        "ig_user_id": "ig-1",
    }


def test_callback_respects_next_path_from_state(
    monkeypatch, client: TestClient
) -> None:
    """Onboarding wizard sends users back to /onboarding/creator;
    settings page sends them back to /creator/profile/settings. The
    signed state carries which."""
    _signed_in(client)
    _stub_verify_state(monkeypatch, next_path="/onboarding/creator")
    monkeypatch.setattr(
        instagram_meta,
        "exchange_code",
        lambda _c: {"access_token": "tok", "expires_in": 5184000},
    )
    monkeypatch.setattr(
        instagram_meta,
        "resolve_business_account",
        lambda _t: instagram_meta.InstagramAccount(
            ig_user_id="ig-1", username=None, name=None
        ),
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "save_instagram_connection",
        lambda *a, **kw: True,
    )

    r = client.get(
        "/creator/instagram/callback?code=abc&state=ok",
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/onboarding/creator?")
    assert "instagram=connected" in r.headers["location"]


# ---------------------------------------------------------------------------
# /creator/instagram/disconnect
# ---------------------------------------------------------------------------


def test_disconnect_requires_creator(client: TestClient) -> None:
    _signed_in(client, role="operator", user_id="op-1")
    r = client.post("/creator/instagram/disconnect", follow_redirects=False)
    assert r.status_code == 403


def test_disconnect_clears_row_and_redirects(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    called = {}
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "disconnect_instagram",
        lambda uid: called.setdefault("uid", uid) or True,
    )
    r = client.post("/creator/instagram/disconnect", follow_redirects=False)
    assert r.status_code == 303
    assert called == {"uid": "creator-1"}
    assert r.headers["location"].endswith("?instagram=disconnected")


# ---------------------------------------------------------------------------
# integrations grid card flip (rendered on profile/settings)
# ---------------------------------------------------------------------------


def _profile() -> dict:
    return {
        "onboarding_completed_at": "2026-05-01T00:00:00Z",
        "full_name": "Mia",
        "instagram_handle": "miacreates",
    }


def test_settings_renders_real_connect_when_configured_and_not_connected(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)

    r = client.get("/creator/profile/settings")
    assert r.status_code == 200
    # Real connect link present, no "coming soon" copy on the IG card.
    assert "/creator/instagram/connect?next=/creator/profile/settings" in r.text
    assert "connect instagram" in r.text


def test_settings_renders_coming_soon_when_not_configured(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)

    r = client.get("/creator/profile/settings")
    assert r.status_code == 200
    assert "/creator/instagram/connect" not in r.text
    assert "coming soon" in r.text


def test_settings_renders_disconnect_when_connected(
    monkeypatch, client: TestClient
) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: {"provider": "instagram", "access_token": "tok"},
    )
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)

    r = client.get("/creator/profile/settings")
    assert r.status_code == 200
    assert 'action="/creator/instagram/disconnect"' in r.text
    assert "disconnect instagram" in r.text


def test_settings_flashes_ineligible_message(
    monkeypatch, client: TestClient
) -> None:
    """The specific creator-facing copy must surface on the page after
    a failed callback so the user knows how to fix their account."""
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)

    r = client.get("/creator/profile/settings?instagram=ineligible")
    assert r.status_code == 200
    # Copy must explain the requirement; with the Instagram Login API
    # flow, the only requirement is a Business or Creator account —
    # no FB Page linking — so the message no longer mentions it.
    assert "Business or Creator account" in r.text
    assert "Facebook Page" not in r.text


# ---------------------------------------------------------------------------
# Slice 3 regression pins.
# These are defensive — they don't change behavior, they freeze it so a
# future refactor can't silently re-enable posting/DMs, flip TikTok to
# "ready", or break the connect link's exact href.
# ---------------------------------------------------------------------------


def test_connect_button_href_is_pinned_on_settings(
    monkeypatch, client: TestClient
) -> None:
    """The exact href is what the integrations grid template ships
    today. Catches accidental drift (e.g. someone changing the `next`
    param or dropping it entirely)."""
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections, "get_google_connection", lambda uid: None
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections, "get_instagram_connection", lambda uid: None
    )
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)

    r = client.get("/creator/profile/settings")
    assert r.status_code == 200
    assert (
        'href="/creator/instagram/connect?next=/creator/profile/settings"'
        in r.text
    )


def test_tiktok_card_stays_coming_soon_in_every_instagram_state(
    monkeypatch, client: TestClient
) -> None:
    """TikTok is not integrated. The grid must render it as coming-soon
    + disabled regardless of how Instagram is configured/connected."""
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile", lambda uid: _profile()
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections, "get_google_connection", lambda uid: None
    )

    scenarios = [
        # (ig configured, ig connection row)
        (False, None),
        (True, None),
        (True, {"provider": "instagram", "access_token": "tok"}),
    ]
    for configured, ig_conn in scenarios:
        monkeypatch.setattr(instagram_meta, "is_configured", lambda c=configured: c)
        monkeypatch.setattr(
            creator_routes.oauth_connections,
            "get_instagram_connection",
            lambda uid, ig_conn=ig_conn: ig_conn,
        )

        r = client.get("/creator/profile/settings")
        assert r.status_code == 200
        # Tiny snapshot: TikTok must be coming soon and disabled, with
        # no `connect Tiktok` CTA anywhere on the page.
        assert "TikTok" in r.text
        # The tag has `disabled aria-disabled="true"` inside the TikTok card.
        # Easiest to assert: there's no real TikTok connect link.
        assert "/creator/tiktok/connect" not in r.text
        assert "coming soon" in r.text


def test_instagram_meta_module_does_not_expose_write_surface() -> None:
    """Belt-and-suspenders pin: even if a future contributor adds a
    posting/DM helper here, this test fails before it ships.

    Mirrors the test_module_does_not_expose_send_delete_or_modify pin
    in tests/test_google_gmail.py for Gmail."""
    forbidden = (
        "post_media",
        "publish_media",
        "create_media",
        "send_dm",
        "create_comment",
        "delete_media",
        "delete_comment",
        "set_media_caption",
    )
    for name in forbidden:
        assert not hasattr(instagram_meta, name), (
            f"instagram_meta must not expose {name}"
        )
