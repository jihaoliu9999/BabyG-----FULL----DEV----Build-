"""Booking CRUD tests — calendar list, create, edit, cancel."""

from __future__ import annotations

from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core.security import SESSION_COOKIE, write_session
from app.integrations import google_calendar as google_calendar_module
from app.main import app
from app.services import abuse as abuse_module
from app.services import bookings as bookings_module
from app.services import calendar_sync as calendar_sync_module
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import notifications as notifications_module
from app.services import oauth_connections as oauth_module


class FakeWorld:
    def __init__(self):
        self.bookings: dict[str, dict[str, Any]] = {}


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    def _list_for_user(uid, *, horizon="all", limit=200):
        rows = [b for b in w.bookings.values() if b["user_id"] == uid and b["status"] != "cancelled"]
        rows.sort(key=lambda b: b["starts_at"])
        return rows

    def _get(bid):
        return w.bookings.get(bid)

    def _create(*, user_id, payload):
        bid = str(uuid4())
        w.bookings[bid] = {
            **payload, "id": bid, "user_id": user_id,
            "created_at": "2026-05-07T00:00:00Z",
        }
        return bid

    def _update(bid, *, user_id, payload):
        b = w.bookings.get(bid)
        if not b or b["user_id"] != user_id:
            return False
        b.update(payload)
        return True

    def _cancel(bid, *, user_id):
        return _update(bid, user_id=user_id, payload={"status": "cancelled"})

    monkeypatch.setattr(bookings_module, "list_for_user", _list_for_user)
    monkeypatch.setattr(bookings_module, "get", _get)
    monkeypatch.setattr(bookings_module, "create", _create)
    monkeypatch.setattr(bookings_module, "update", _update)
    monkeypatch.setattr(bookings_module, "cancel", _cancel)

    monkeypatch.setattr(oauth_module, "get_google_connection", lambda uid: None)
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: False)

    # Quiet everything else
    monkeypatch.setattr(notifications_module, "create", lambda **kw: True)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "feedback_for_user", lambda uid, ids: {})
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)
    return w


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def _signed_in(client, *, role, user_id):
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def test_calendar_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-1", "title": "Dinner at Boia",
        "type": "restaurant", "starts_at": "2099-05-07T19:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": "Boia De",
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/creator/calendar")
    assert r.status_code == 200
    assert "Dinner at Boia" in r.text


def test_calendar_create(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar",
        data={
            "title": "Brand call",
            "type": "brand",
            "starts_at": "2099-05-08T10:00",
            "ends_at": "",
            "notes": "Pitch the SS26 capsule.",
            "venue_name": "Zoom",
            "status": "confirmed",
        },
    )
    assert r.status_code == 303
    assert len(world.bookings) == 1
    b = next(iter(world.bookings.values()))
    assert b["title"] == "Brand call"
    assert b["type"] == "brand"


def test_calendar_create_rejects_missing_title(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar",
        data={"type": "event", "starts_at": "2099-05-08T10:00"},
    )
    assert r.status_code == 400


def test_calendar_create_rejects_unknown_type(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar",
        data={"title": "x", "type": "garbage", "starts_at": "2099-05-08T10:00"},
    )
    assert r.status_code == 400


def test_calendar_detail_only_owner(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-other", "title": "Theirs",
        "type": "event", "starts_at": "2099-05-08T10:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": None,
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get(f"/creator/calendar/{bid}")
    assert r.status_code == 404


def test_calendar_cancel(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-1", "title": "Skip this",
        "type": "event", "starts_at": "2099-05-08T10:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": None,
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(f"/creator/calendar/{bid}/cancel")
    assert r.status_code == 303
    assert world.bookings[bid]["status"] == "cancelled"


def test_calendar_requires_creator(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/calendar")
    assert r.status_code == 403


def test_calendar_google_connect_redirects_to_google(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)

    r = client.get("/creator/google/calendar/connect")

    assert r.status_code == 302
    assert r.headers["location"] == (
        "/creator/google/connect?service=calendar&next=/creator/calendar"
    )


def test_google_connect_picker_preselects_calendar(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)

    r = client.get("/creator/google/connect?service=calendar")

    assert r.status_code == 200
    assert 'name="services" value="calendar"' in r.text
    assert 'name="services" value="calendar" style="margin-top:3px;" checked' in r.text
    assert 'name="services" value="gmail" style="margin-top:3px;" checked' not in r.text


def test_google_connect_picker_preselects_gmail(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)

    r = client.get("/creator/google/connect?service=gmail")

    assert r.status_code == 200
    assert 'name="services" value="gmail"' in r.text
    assert 'name="services" value="gmail" style="margin-top:3px;" checked' in r.text
    assert 'name="services" value="calendar" style="margin-top:3px;" checked' not in r.text


def test_google_connect_requires_one_service(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)

    r = client.post(
        "/creator/google/connect",
        data={"next_path": "/creator/profile/settings"},
    )

    assert r.status_code == 400
    assert "choose at least one Google service." in r.text


@pytest.mark.parametrize(
    ("services", "expected_services", "expected_scopes", "unexpected_scope"),
    [
        (
            ["calendar"],
            ["calendar"],
            [google_calendar_module.CALENDAR_SCOPE],
            google_calendar_module.GMAIL_READONLY_SCOPE,
        ),
        (
            ["gmail"],
            ["gmail"],
            [google_calendar_module.GMAIL_READONLY_SCOPE],
            google_calendar_module.CALENDAR_SCOPE,
        ),
        (
            ["calendar", "gmail"],
            ["calendar", "gmail"],
            [
                google_calendar_module.CALENDAR_SCOPE,
                google_calendar_module.GMAIL_READONLY_SCOPE,
            ],
            None,
        ),
    ],
)
def test_google_connect_post_redirects_to_oauth_with_selected_scopes(
    client,
    world,
    monkeypatch,
    services,
    expected_services,
    expected_scopes,
    unexpected_scope,
):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)

    r = client.post(
        "/creator/google/connect",
        data={
            "services": services,
            "next_path": "/creator/profile/settings",
        },
    )

    assert r.status_code == 302
    parsed = urlparse(r.headers["location"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    query = parse_qs(parsed.query)
    assert query["scope"] == [" ".join(expected_scopes)]
    if unexpected_scope:
        assert unexpected_scope not in query["scope"][0]
    state = oauth_module.verify_google_state(query["state"][0])
    assert state is not None
    assert state["services"] == expected_services
    assert state["scopes"] == expected_scopes


def test_google_connect_posts_selected_services_and_scopes(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)
    calls: dict[str, Any] = {}

    def _auth_url(state, *, scopes_override=None):
        calls["state"] = oauth_module.verify_google_state(state)
        calls["scopes"] = scopes_override
        return "https://accounts.google.com/o/oauth2/v2/auth?ok=1"

    monkeypatch.setattr(google_calendar_module, "auth_url", _auth_url)

    r = client.post(
        "/creator/google/connect",
        data={
            "services": ["calendar", "gmail"],
            "next_path": "/creator/profile/settings",
        },
    )

    assert r.status_code == 302
    assert r.headers["location"].endswith("ok=1")
    assert calls["state"]["services"] == ["calendar", "gmail"]
    assert calls["state"]["scopes"] == [
        google_calendar_module.CALENDAR_SCOPE,
        google_calendar_module.GMAIL_READONLY_SCOPE,
    ]
    assert calls["scopes"] == calls["state"]["scopes"]


def test_google_auth_url_uses_only_selected_scopes(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret")
    get_settings.cache_clear()

    url = google_calendar_module.auth_url(
        "signed-state",
        scopes_override=[google_calendar_module.CALENDAR_SCOPE],
    )

    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [google_calendar_module.CALENDAR_SCOPE]
    assert "gmail" not in query["scope"][0]


def test_calendar_google_callback_saves_and_syncs(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        oauth_module,
        "verify_google_state",
        lambda state: {
            "user_id": "c-1",
            "next": "/creator/calendar",
            "services": ["calendar"],
            "scopes": [google_calendar_module.CALENDAR_SCOPE],
        },
    )
    monkeypatch.setattr(
        google_calendar_module,
        "exchange_code",
        lambda code: {"access_token": "access", "refresh_token": "refresh"},
    )

    def _save(uid, token_response, *, requested_scopes=None):
        calls["saved"] = (uid, token_response, requested_scopes)
        return True

    def _sync(uid):
        calls["synced"] = uid
        return calendar_sync_module.CalendarSyncResult(imported=2, connected=True)

    monkeypatch.setattr(oauth_module, "save_google_connection", _save)
    monkeypatch.setattr(calendar_sync_module, "sync_google_calendar", _sync)

    r = client.get("/creator/google/calendar/callback?code=abc&state=signed")

    assert r.status_code == 303
    assert r.headers["location"] == "/creator/calendar?google=connected&synced=2"
    assert calls["saved"][0] == "c-1"
    assert calls["saved"][2] == [google_calendar_module.CALENDAR_SCOPE]
    assert calls["synced"] == "c-1"


def test_google_gmail_only_callback_does_not_sync_calendar(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
    calls: dict[str, Any] = {}

    monkeypatch.setattr(
        oauth_module,
        "verify_google_state",
        lambda state: {
            "user_id": "c-1",
            "next": "/creator/profile/settings",
            "services": ["gmail"],
            "scopes": [google_calendar_module.GMAIL_READONLY_SCOPE],
        },
    )
    monkeypatch.setattr(
        google_calendar_module,
        "exchange_code",
        lambda code: {"access_token": "access", "refresh_token": "refresh"},
    )

    def _save(uid, token_response, *, requested_scopes=None):
        calls["saved"] = (uid, requested_scopes)
        return True

    def _sync(uid):
        raise AssertionError("gmail-only callback must not sync calendar")

    monkeypatch.setattr(oauth_module, "save_google_connection", _save)
    monkeypatch.setattr(calendar_sync_module, "sync_google_calendar", _sync)

    r = client.get("/creator/google/calendar/callback?code=abc&state=signed")

    assert r.status_code == 303
    assert r.headers["location"] == "/creator/profile/settings?google=connected"
    assert calls["saved"] == ("c-1", [google_calendar_module.GMAIL_READONLY_SCOPE])


def test_google_both_callback_saves_both_and_syncs_calendar(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
    calls: dict[str, Any] = {}

    requested = [
        google_calendar_module.CALENDAR_SCOPE,
        google_calendar_module.GMAIL_READONLY_SCOPE,
    ]
    monkeypatch.setattr(
        oauth_module,
        "verify_google_state",
        lambda state: {
            "user_id": "c-1",
            "next": "/creator/calendar",
            "services": ["calendar", "gmail"],
            "scopes": requested,
        },
    )
    monkeypatch.setattr(
        google_calendar_module,
        "exchange_code",
        lambda code: {"access_token": "access", "refresh_token": "refresh"},
    )
    monkeypatch.setattr(
        oauth_module,
        "save_google_connection",
        lambda uid, token_response, *, requested_scopes=None: calls.setdefault(
            "saved", requested_scopes
        )
        is not None,
    )
    monkeypatch.setattr(
        calendar_sync_module,
        "sync_google_calendar",
        lambda uid: calendar_sync_module.CalendarSyncResult(imported=3, connected=True),
    )

    r = client.get("/creator/google/calendar/callback?code=abc&state=signed")

    assert r.status_code == 303
    assert r.headers["location"] == "/creator/calendar?google=connected&synced=3"
    assert calls["saved"] == requested


def test_calendar_google_callback_rejects_other_user_state(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(
        oauth_module,
        "verify_google_state",
        lambda state: {"user_id": "c-other", "next": "/creator/calendar"},
    )

    r = client.get("/creator/google/calendar/callback?code=abc&state=signed")

    assert r.status_code == 403


def test_calendar_google_sync_now(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    calls = []
    monkeypatch.setattr(
        oauth_module,
        "get_google_connection",
        lambda uid: {"scopes": [google_calendar_module.CALENDAR_SCOPE]},
    )

    def _sync(uid):
        calls.append(uid)
        return calendar_sync_module.CalendarSyncResult(imported=1, connected=True)

    monkeypatch.setattr(calendar_sync_module, "sync_google_calendar", _sync)

    r = client.post("/creator/google/calendar/sync")

    assert r.status_code == 303
    assert r.headers["location"] == "/creator/calendar?sync=done&synced=1"
    assert calls == ["c-1"]


def test_google_refresh_save_preserves_existing_scopes(monkeypatch):
    captured: dict[str, Any] = {}
    existing_scopes = [
        google_calendar_module.CALENDAR_SCOPE,
        google_calendar_module.GMAIL_READONLY_SCOPE,
    ]
    monkeypatch.setattr(
        oauth_module,
        "get_google_connection",
        lambda uid: {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "scopes": existing_scopes,
        },
    )

    class _Table:
        def upsert(self, payload, *, on_conflict):
            captured["payload"] = payload
            captured["on_conflict"] = on_conflict
            return self

        def execute(self):
            return None

    class _Client:
        def table(self, name):
            captured["table"] = name
            return _Table()

    monkeypatch.setattr(
        oauth_module.supabase_client,
        "get_service_client",
        lambda: _Client(),
    )

    assert oauth_module.save_google_connection(
        "c-1",
        {"access_token": "new-access", "expires_in": 3600},
    )
    assert captured["table"] == "oauth_connections"
    assert captured["on_conflict"] == "user_id,provider"
    assert captured["payload"]["scopes"] == existing_scopes
    assert captured["payload"]["refresh_token"] == "old-refresh"
