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

    def _list_for_user_range(uid, **_kwargs):
        return _list_for_user(uid)

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
    monkeypatch.setattr(bookings_module, "list_for_user_range", _list_for_user_range)
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
    r = client.get("/creator/calendar?view=day&date=2099-05-07")
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


def test_calendar_list_is_month_only(client, world):
    """/creator/calendar is now a month-only page. Day/week views,
    the view selector, the hourly grid, the header add-item button,
    and the sync/disconnect footer are all removed. A month event
    still renders as a preview inside its cell."""
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-1", "title": "Month event",
        "type": "event", "starts_at": "2099-05-07T14:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": None,
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/creator/calendar?date=2099-05-07")
    assert r.status_code == 200
    body = r.text
    # Month-only wrapper renders and the event surfaces inside its cell.
    assert "calendar-month-only-page" in body
    assert "calendar-month-grid" in body
    assert "Month event" in body
    # Day/week/hourly-grid markup is gone.
    assert "calendar-week-shell" not in body
    assert "calendar-week-head" not in body
    assert "calendar-time-grid" not in body
    assert "calendar-view-tabs" not in body
    assert "calendar-mobile-list" not in body
    # Header no longer carries the `add item` button or the
    # sync/disconnect footer actions.
    assert "/creator/calendar/new" not in body
    assert "/creator/google/calendar/sync" not in body
    assert "/creator/google/calendar/disconnect" not in body


def test_calendar_list_header_has_prev_today_next_nav(client, world):
    """Header ROW 2 = month title + previous/today/next controls only."""
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/creator/calendar?date=2099-05-07")
    assert r.status_code == 200
    assert "calendar-month-nav-btn" in r.text
    # previous / today / next anchors are present with matching hrefs.
    assert 'href="/creator/calendar?date=2099-04-01"' in r.text
    assert 'href="/creator/calendar?date=2099-06-01"' in r.text
    assert 'href="/creator/calendar"' in r.text


def test_calendar_list_emits_bottom_sheet_markup(client, world):
    """Tapping a day opens a bottom sheet — the template must emit
    both the day-detail and add-event sheet containers plus the JSON
    events payload for the JS to render into the day sheet."""
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/creator/calendar?date=2099-05-07")
    assert r.status_code == 200
    body = r.text
    assert 'data-cal-sheet="day"' in body
    assert 'data-cal-sheet="add"' in body
    assert 'id="calendar-month-events-data"' in body
    assert 'data-cal-month-grid' in body
    assert '/static/js/creator_calendar_month.js' in body


def test_calendar_list_month_cell_shows_max_two_event_previews(client, world):
    """A month cell renders at most 2 event chips + a `+N` overflow."""
    _signed_in(client, role="creator", user_id="c-1")
    for i in range(5):
        bid = str(uuid4())
        world.bookings[bid] = {
            "id": bid, "user_id": "c-1", "title": f"Evt {i}",
            "type": "event",
            "starts_at": f"2099-05-07T{10 + i:02d}:00:00Z",
            "ends_at": None, "status": "confirmed", "venue_name": None,
            "notes": None, "created_at": "2026-05-07T00:00:00Z",
        }
    r = client.get("/creator/calendar?date=2099-05-07")
    assert r.status_code == 200
    # +3 overflow chip appears for 5 events - 2 shown = 3 hidden.
    assert "+3" in r.text
    assert "calendar-month-cell-more" in r.text


def test_calendar_quick_add_requires_title(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar/quick-add",
        data={"title": "", "date": "2099-05-07", "time": "10:00"},
    )
    assert r.status_code == 400


def test_calendar_quick_add_requires_time_when_not_all_day(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar/quick-add",
        data={"title": "Test", "date": "2099-05-07"},
    )
    assert r.status_code == 400


def test_calendar_quick_add_all_day_persists_local_booking(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar/quick-add",
        data={
            "title": "All-day thing",
            "date": "2099-05-07",
            "all_day": "1",
        },
    )
    assert r.status_code == 303
    assert any(
        b["title"] == "All-day thing" and b.get("is_all_day")
        for b in world.bookings.values()
    )


# ---- Google-connected quick-add → real Google write path ----------
# When the user's Google calendar is connected, BOTH timed and all-day
# events must flow through google_calendar.create_primary_event and
# then be persisted via upsert_google_event on the returned event id.
# The all-day path uses Google's start.date / end.date payload shape
# and never round-trips through UTC midnight — that's the shift bug
# the sync fix already landed for reads; this write path preserves it.


def _wire_google_connected(monkeypatch):
    """Route a quick-add through the Google-connected branch. Returns
    a captured-calls dict so each test can assert what was written."""
    calls: dict[str, Any] = {
        "google_payload": None,
        "google_kwargs": None,
        "upsert_payload": None,
        "local_create_payload": None,
    }
    monkeypatch.setattr(
        oauth_module,
        "get_google_connection",
        lambda uid: {"provider": "google", "access_token": "tok"},
    )
    monkeypatch.setattr(
        oauth_module, "google_calendar_connected", lambda conn: True
    )
    monkeypatch.setattr(
        oauth_module, "access_token_for_google", lambda uid: "tok"
    )
    monkeypatch.setattr(
        calendar_sync_module, "effective_timezone", lambda uid: "America/New_York"
    )
    monkeypatch.setattr(calendar_sync_module, "maybe_auto_sync", lambda uid: None)

    def _create_primary_event(token, **kwargs):
        calls["google_kwargs"] = kwargs
        return "gcal-created-evt-1"

    monkeypatch.setattr(
        google_calendar_module, "create_primary_event", _create_primary_event
    )

    def _upsert(*, user_id, payload):
        calls["upsert_payload"] = {**payload, "user_id": user_id}
        return True

    monkeypatch.setattr(bookings_module, "upsert_google_event", _upsert)

    def _local_create(*, user_id, payload):
        calls["local_create_payload"] = {**payload, "user_id": user_id}
        return "local-should-not-happen"

    monkeypatch.setattr(bookings_module, "create", _local_create)
    return calls


def test_calendar_quick_add_google_timed_event_writes_to_google(
    client, world, monkeypatch
):
    calls = _wire_google_connected(monkeypatch)
    _signed_in(client, role="creator", user_id="c-1")

    r = client.post(
        "/creator/calendar/quick-add",
        data={
            "title": "Brand call",
            "date": "2099-05-07",
            "time": "10:00",
        },
    )

    assert r.status_code == 303
    # Timed event went through the Google write path.
    assert calls["google_kwargs"] is not None
    assert calls["google_kwargs"]["title"] == "Brand call"
    assert not calls["google_kwargs"].get("all_day")
    # Google's returned event id was persisted through the canonical
    # google-synced upsert path.
    assert calls["upsert_payload"]["google_event_id"] == "gcal-created-evt-1"
    assert calls["upsert_payload"]["google_calendar_id"] == "primary"
    # No parallel local-only row was created.
    assert calls["local_create_payload"] is None


def test_calendar_quick_add_google_all_day_event_writes_to_google(
    client, world, monkeypatch
):
    calls = _wire_google_connected(monkeypatch)
    _signed_in(client, role="creator", user_id="c-1")

    r = client.post(
        "/creator/calendar/quick-add",
        data={
            "title": "Shoot day",
            "date": "2099-05-07",
            "all_day": "1",
        },
    )

    assert r.status_code == 303
    # All-day event STILL routed through the Google write path.
    assert calls["google_kwargs"] is not None
    assert calls["google_kwargs"]["all_day"] is True
    # Google's all-day payload uses ISO dates (start.date / end.date
    # semantics), never UTC datetimes.
    assert calls["google_kwargs"]["starts_at"] == "2099-05-07"
    # Google's end.date is exclusive (day after last day of event).
    assert calls["google_kwargs"]["ends_at"] == "2099-05-08"
    # Persisted through upsert path (the canonical google-synced row),
    # NOT the local-only bookings.create fallback.
    assert calls["upsert_payload"] is not None
    assert calls["upsert_payload"]["google_event_id"] == "gcal-created-evt-1"
    assert calls["upsert_payload"]["is_all_day"] is True
    assert calls["local_create_payload"] is None


def test_calendar_quick_add_google_all_day_preserves_selected_calendar_date(
    client, world, monkeypatch
):
    """All-day event on Sep 7 must stay on Sep 7 in the user's calendar
    timezone. The stored timestamptz anchors midnight to the calendar
    zone so the local calendar date matches the day the user tapped."""
    calls = _wire_google_connected(monkeypatch)
    _signed_in(client, role="creator", user_id="c-1")

    r = client.post(
        "/creator/calendar/quick-add",
        data={
            "title": "Shoot day",
            "date": "2099-05-07",
            "all_day": "1",
        },
    )

    assert r.status_code == 303
    # Google sees the raw selected date — no UTC round-trip.
    assert calls["google_kwargs"]["starts_at"] == "2099-05-07"
    # The upsert row's stored timestamptz anchors midnight to the
    # user's calendar zone (America/New_York in this test), so when
    # re-interpreted at render time in the same zone the local
    # calendar date resolves back to Sep 7 rather than sliding back
    # to Sep 6. In EDT (UTC-4) that stores as 04:00Z; DST-independent
    # check: the UTC time is on or after 04:00Z on Sep 7 and before
    # 06:00Z on Sep 7, which excludes both Sep 6 and Sep 8.
    stored = calls["upsert_payload"]["starts_at"]
    assert stored.startswith("2099-05-07T0")
    assert not stored.startswith("2099-05-06")
    assert not stored.startswith("2099-05-08")
    # Redirect lands on Sep 7 so the month view stays parked there.
    assert r.headers["location"].endswith("?date=2099-05-07")


def test_calendar_quick_add_google_all_day_never_creates_duplicate_local_row(
    client, world, monkeypatch
):
    """Guard against the earlier bug where all-day fell through to a
    local-only bookings.create. Only upsert_google_event must fire
    when Google is connected — never both."""
    calls = _wire_google_connected(monkeypatch)
    _signed_in(client, role="creator", user_id="c-1")

    client.post(
        "/creator/calendar/quick-add",
        data={
            "title": "Shoot day",
            "date": "2099-05-07",
            "all_day": "1",
        },
    )

    assert calls["upsert_payload"] is not None
    assert calls["local_create_payload"] is None


def test_google_calendar_create_primary_event_all_day_payload_shape(monkeypatch):
    """Lock the Google-side payload shape for all-day events: uses
    start.date / end.date, never start.dateTime / end.dateTime, and
    end.date is Google's exclusive next-day."""
    captured: dict[str, Any] = {}

    def _post(url, *, json, headers, timeout):
        captured["json"] = json
        return _CalendarResp(200, {"id": "gcal-all-day"})

    monkeypatch.setattr(google_calendar_module.httpx, "post", _post)

    event_id = google_calendar_module.create_primary_event(
        "tok",
        title="Shoot day",
        starts_at="2099-05-07",
        ends_at="2099-05-08",
        all_day=True,
    )

    assert event_id == "gcal-all-day"
    payload = captured["json"]
    assert payload["start"] == {"date": "2099-05-07"}
    assert payload["end"] == {"date": "2099-05-08"}
    assert "dateTime" not in payload["start"]
    assert "dateTime" not in payload["end"]


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
    assert 'id="google-service-calendar"' in r.text
    assert 'for="google-service-calendar"' in r.text
    assert 'id="google-service-calendar" type="checkbox" name="calendar" value="1" style="margin-top:3px;" checked' in r.text
    assert 'id="google-service-gmail" type="checkbox" name="gmail" value="1" style="margin-top:3px;" checked' not in r.text


def test_google_connect_picker_preselects_gmail(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)

    r = client.get("/creator/google/connect?service=gmail")

    assert r.status_code == 200
    assert 'id="google-service-gmail"' in r.text
    assert 'for="google-service-gmail"' in r.text
    assert 'id="google-service-gmail" type="checkbox" name="gmail" value="1" style="margin-top:3px;" checked' in r.text
    assert 'id="google-service-calendar" type="checkbox" name="calendar" value="1" style="margin-top:3px;" checked' not in r.text


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
    ("service", "expected"),
    [
        ("calendar", ["calendar"]),
        ("gmail", ["gmail"]),
    ],
)
def test_google_connect_prechecked_service_submits_expected_service(
    client, world, monkeypatch, service, expected
):
    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: True)
    captured: dict[str, Any] = {}

    def _auth_url(state, *, scopes_override=None):
        captured["state"] = oauth_module.verify_google_state(state)
        return "https://accounts.google.com/o/oauth2/v2/auth?ok=1"

    monkeypatch.setattr(google_calendar_module, "auth_url", _auth_url)

    r = client.post(
        "/creator/google/connect",
        data={
            **{service: "1" for service in expected},
            "next_path": "/creator/profile/settings",
        },
    )

    assert r.status_code == 302
    assert captured["state"]["services"] == expected


@pytest.mark.parametrize(
    ("form_flags", "expected_services", "expected_scopes", "unexpected_scope"),
    [
        (
            {"calendar": "1"},
            ["calendar"],
            [google_calendar_module.CALENDAR_SCOPE],
            google_calendar_module.GMAIL_COMPOSE_SCOPE,
        ),
        (
            {"gmail": "1"},
            ["gmail"],
            [
                google_calendar_module.GMAIL_READONLY_SCOPE,
                google_calendar_module.GMAIL_COMPOSE_SCOPE,
                google_calendar_module.GMAIL_SEND_SCOPE,
            ],
            google_calendar_module.CALENDAR_SCOPE,
        ),
        (
            {"calendar": "1", "gmail": "1"},
            ["calendar", "gmail"],
            [
                google_calendar_module.CALENDAR_SCOPE,
                google_calendar_module.GMAIL_READONLY_SCOPE,
                google_calendar_module.GMAIL_COMPOSE_SCOPE,
                google_calendar_module.GMAIL_SEND_SCOPE,
            ],
            None,
        ),
    ],
)
def test_google_connect_post_redirects_to_oauth_with_selected_scopes(
    client,
    world,
    monkeypatch,
    form_flags,
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
            **form_flags,
            "next_path": "/creator/profile/settings",
        },
    )

    assert r.status_code == 302
    parsed = urlparse(r.headers["location"])
    assert parsed.scheme == "https"
    assert parsed.netloc == "accounts.google.com"
    query = parse_qs(parsed.query)
    # Pin every OAuth query parameter Google requires. Drops are silent
    # at the Google end (consent screen errors, redirect mismatches),
    # so the test catches regressions before they ship.
    assert query["client_id"] == ["client-id"]
    assert query["redirect_uri"] == [google_calendar_module.redirect_uri()]
    assert query["response_type"] == ["code"]
    assert query["access_type"] == ["offline"]
    assert query["prompt"] == ["consent"]
    assert query["include_granted_scopes"] == ["true"]
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
            "calendar": "1",
            "gmail": "1",
            "next_path": "/creator/profile/settings",
        },
    )

    assert r.status_code == 302
    assert r.headers["location"].endswith("ok=1")
    assert calls["state"]["services"] == ["calendar", "gmail"]
    assert calls["state"]["scopes"] == [
        google_calendar_module.CALENDAR_SCOPE,
        google_calendar_module.GMAIL_READONLY_SCOPE,
        google_calendar_module.GMAIL_COMPOSE_SCOPE,
        google_calendar_module.GMAIL_SEND_SCOPE,
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


def test_google_scopes_ignore_broad_env_configuration(monkeypatch):
    monkeypatch.setenv(
        "GOOGLE_OAUTH_SCOPES",
        "https://www.googleapis.com/auth/calendar "
        "https://mail.google.com/ "
        "https://www.googleapis.com/auth/gmail.modify",
    )
    get_settings.cache_clear()

    assert google_calendar_module.scopes_for_services(["calendar", "gmail"]) == [
        google_calendar_module.CALENDAR_SCOPE,
        google_calendar_module.GMAIL_READONLY_SCOPE,
        google_calendar_module.GMAIL_COMPOSE_SCOPE,
        google_calendar_module.GMAIL_SEND_SCOPE,
    ]


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
            "scopes": [
                google_calendar_module.GMAIL_READONLY_SCOPE,
                google_calendar_module.GMAIL_COMPOSE_SCOPE,
                google_calendar_module.GMAIL_SEND_SCOPE,
            ],
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
    assert calls["saved"] == (
        "c-1",
        [
            google_calendar_module.GMAIL_READONLY_SCOPE,
            google_calendar_module.GMAIL_COMPOSE_SCOPE,
            google_calendar_module.GMAIL_SEND_SCOPE,
        ],
    )


def test_google_both_callback_saves_both_and_syncs_calendar(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
    calls: dict[str, Any] = {}

    requested = [
        google_calendar_module.CALENDAR_SCOPE,
        google_calendar_module.GMAIL_READONLY_SCOPE,
        google_calendar_module.GMAIL_COMPOSE_SCOPE,
        google_calendar_module.GMAIL_SEND_SCOPE,
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


@pytest.mark.parametrize(
    ("query", "expected_location"),
    [
        (
            "error=access_denied&state=signed",
            "/creator/profile/settings?google=denied",
        ),
        ("state=signed", "/creator/profile/settings?google=bad_callback"),
    ],
)
def test_google_callback_returns_to_signed_next_on_cancel_or_bad_callback(
    client, world, monkeypatch, query, expected_location
):
    _signed_in(client, role="creator", user_id="c-1")
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

    r = client.get(f"/creator/google/calendar/callback?{query}")

    assert r.status_code == 303
    assert r.headers["location"] == expected_location


def test_google_callback_exchange_failure_returns_to_signed_next(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
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
        lambda code: (_ for _ in ()).throw(
            google_calendar_module.GoogleCalendarError("exchange failed")
        ),
    )

    r = client.get("/creator/google/calendar/callback?code=abc&state=signed")

    assert r.status_code == 303
    assert r.headers["location"] == "/creator/profile/settings?google=exchange_failed"


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
        google_calendar_module.GMAIL_COMPOSE_SCOPE,
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


class _CalendarResp:
    def __init__(self, status_code: int, payload: dict[str, Any]):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise google_calendar_module.httpx.HTTPStatusError(
                "bad",
                request=google_calendar_module.httpx.Request(
                    "POST", google_calendar_module.EVENTS_URL
                ),
                response=google_calendar_module.httpx.Response(self.status_code),
            )

    def json(self):
        return self._payload


def test_google_calendar_create_primary_event_posts_sanitized_payload(monkeypatch):
    calls: list[dict[str, Any]] = []

    def _post(url, *, json, headers, timeout):
        calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        return _CalendarResp(200, {"id": "gcal-1"})

    monkeypatch.setattr(google_calendar_module.httpx, "post", _post)

    event_id = google_calendar_module.create_primary_event(
        "tok",
        title="  Brand call  ",
        starts_at="2099-05-08T10:00:00Z",
        ends_at="2099-05-08T10:30:00Z",
        notes="Pitch the capsule.",
        location="Zoom",
    )

    assert event_id == "gcal-1"
    call = calls[0]
    assert call["url"] == google_calendar_module.EVENTS_URL
    assert call["headers"]["Authorization"] == "Bearer tok"
    assert call["json"]["summary"] == "Brand call"
    assert call["json"]["start"]["dateTime"] == "2099-05-08T10:00:00Z"
    assert call["json"]["end"]["dateTime"] == "2099-05-08T10:30:00Z"
    assert call["json"]["description"] == "Pitch the capsule."
    assert call["json"]["location"] == "Zoom"


def test_google_calendar_create_primary_event_defaults_one_hour_end(monkeypatch):
    calls: list[dict[str, Any]] = []

    def _post(url, *, json, headers, timeout):
        calls.append({"json": json})
        return _CalendarResp(200, {"id": "gcal-2"})

    monkeypatch.setattr(google_calendar_module.httpx, "post", _post)

    google_calendar_module.create_primary_event(
        "tok",
        title="Content day",
        starts_at="2099-05-08T10:00:00Z",
    )

    assert calls[0]["json"]["end"]["dateTime"] == "2099-05-08T11:00:00Z"


def test_google_calendar_create_primary_event_failure_logs_status_only(
    monkeypatch, caplog
):
    secret_token = "CAL-SECRET-TOKEN"
    secret_title = "CAL-SECRET-TITLE"
    secret_notes = "CAL-SECRET-NOTES"

    def _post(url, *, json, headers, timeout):
        return _CalendarResp(500, {"error": "oops"})

    monkeypatch.setattr(google_calendar_module.httpx, "post", _post)
    caplog.set_level("INFO", logger=google_calendar_module.logger.name)

    with pytest.raises(google_calendar_module.GoogleCalendarError):
        google_calendar_module.create_primary_event(
            secret_token,
            title=secret_title,
            starts_at="2099-05-08T10:00:00Z",
            notes=secret_notes,
        )

    for record in caplog.records:
        msg = record.getMessage()
        assert secret_token not in msg
        assert secret_title not in msg
        assert secret_notes not in msg
