"""Home v5 — mobile-canonical spec conformance.

The home v5 layout is a fixed three-section stack on every viewport:

  1. brief
  2. calendar
  3. connected

These tests lock in the section contract + real-data-only rules.
Service calls are stubbed so we never hit Supabase.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes
from app.services import (
    action_proposals as action_proposals_module,
)
from app.services import (
    agent_recap as agent_recap_module,
)
from app.services import (
    bookings as bookings_module,
)
from app.services import (
    brief as brief_module,
)
from app.services import (
    calendar_sync as calendar_sync_module,
)
from app.services import (
    discover as discover_module,
)
from app.services import (
    dms as dms_module,
)
from app.services import (
    home_briefing as home_briefing_module,
)
from app.services import (
    instagram_dms as instagram_dms_module,
)
from app.services import (
    intel as intel_module,
)
from app.services import (
    network as network_module,
)
from app.services import (
    notifications as notifications_module,
)
from app.services import (
    oauth_connections as oauth_module,
)
from app.services import (
    profiles as profiles_module,
)
from app.services import (
    stats_merge as stats_merge_module,
)

# Portable repo-root anchor. This test file lives at
# tests/test_home_calendar_preview.py, so parents[1] is the repo
# root regardless of the checkout location (local dev laptops,
# GitHub Actions, Railway CI, docker builds).
REPO_ROOT = Path(__file__).resolve().parents[1]
APP_CSS = REPO_ROOT / "app" / "static" / "css" / "app.css"
HOME_CALENDAR_JS = REPO_ROOT / "app" / "static" / "js" / "creator_home_calendar.js"


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, user_id: str = "u-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _profile() -> dict[str, Any]:
    return {
        "user_id": "u-1",
        "full_name": "Anna",
        "niches": ["food"],
        "tier": "basic",
        "onboarding_completed_at": "2026-05-07T00:00:00Z",
    }


@pytest.fixture()
def stub_dashboard(monkeypatch):
    """Lean defaults that let /creator render without hitting Supabase.

    Every value can be overridden by writing into the returned dict
    before the request fires — the stubbed lambdas read the live dict.
    """
    state: dict[str, Any] = {
        "profile": _profile(),
        "bookings": [],
        "google_calendar_connected": False,
        "google_gmail_connected": False,
        "instagram_connected": False,
        "instagram_needs_reconnect": False,
        "matched_picks": [],
        "pending_actions": [],
        "pending_connections": [],
        "unread_dms": 0,
        "ig_dm_unread": 0,
        "handled_today": 0,
        "overnight_recap": None,
        "brief_rows": [],
        "unread_notifs": [],
        "performance_view": stats_merge_module.PerformanceView(
            rows=[],
            instagram_status=stats_merge_module.IG_STATUS_NOT_CONNECTED,
        ),
    }
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: state["profile"]
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creator_profile_cached",
        lambda uid, request=None: state["profile"],
    )
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(
        notifications_module,
        "list_unread",
        lambda uid, *, limit=8: list(state["unread_notifs"]),
    )
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(
        dms_module, "unread_count_for_user", lambda uid: state["unread_dms"]
    )
    # Home v5 carousel reads native DMs via list_threads_for_user +
    # unread_counts_by_thread + last_messages_by_thread. Default all
    # three to empty so tests that don't care about native DMs don't
    # accidentally hit supabase.
    monkeypatch.setattr(dms_module, "list_threads_for_user", lambda uid: [])
    monkeypatch.setattr(
        dms_module, "unread_counts_by_thread", lambda uid, ids: {}
    )
    monkeypatch.setattr(
        dms_module, "last_messages_by_thread", lambda ids: {}
    )
    monkeypatch.setattr(
        network_module,
        "list_incoming_pending",
        lambda uid: list(state["pending_connections"]),
    )
    monkeypatch.setattr(
        discover_module,
        "list_cards",
        lambda **kw: list(state["matched_picks"]),
    )
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda **kw: list(state["pending_actions"]),
    )
    monkeypatch.setattr(
        brief_module,
        "home_preview_rows",
        lambda uid: list(state["brief_rows"]),
    )
    monkeypatch.setattr(
        agent_recap_module,
        "build",
        lambda uid: state["overnight_recap"],
    )
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: list(state["bookings"]),
    )
    monkeypatch.setattr(
        bookings_module,
        "list_for_user_range",
        lambda uid, **kw: list(state["bookings"]),
    )
    monkeypatch.setattr(
        oauth_module, "get_google_connection",
        lambda uid: {"connected": True} if (
            state["google_calendar_connected"] or state["google_gmail_connected"]
        ) else None,
    )
    monkeypatch.setattr(
        oauth_module, "google_calendar_connected",
        lambda conn: bool(state["google_calendar_connected"]),
    )
    monkeypatch.setattr(
        oauth_module, "google_gmail_connected",
        lambda conn: bool(state["google_gmail_connected"]),
    )
    monkeypatch.setattr(
        oauth_module, "get_instagram_connection",
        lambda uid: {"access_token": "tok"} if state["instagram_connected"] else None,
    )
    monkeypatch.setattr(
        oauth_module, "instagram_needs_reconnect",
        lambda uid: bool(state["instagram_needs_reconnect"]),
    )
    monkeypatch.setattr(
        instagram_dms_module,
        "unread_count_for_creator",
        lambda uid: state["ig_dm_unread"],
    )
    monkeypatch.setattr(
        home_briefing_module,
        "handled_today",
        lambda uid, **kw: state["handled_today"],
    )
    monkeypatch.setattr(
        stats_merge_module,
        "performance_view",
        lambda uid, **kw: state["performance_view"],
    )
    # Track calls to the calendar auto-sync so tests can assert whether
    # Home actually triggered a freshness refresh. Tests can flip
    # `state["auto_sync_should_fire"]` to see whether the route calls
    # into it.
    calendar_sync_module._LAST_AUTO_SYNC_AT.clear()
    auto_sync_calls: list[str] = []
    state["auto_sync_calls"] = auto_sync_calls

    def _stub_maybe_auto_sync(uid: str):
        auto_sync_calls.append(uid)
        return calendar_sync_module.CalendarSyncResult(
            imported=0, connected=True
        )

    monkeypatch.setattr(calendar_sync_module, "maybe_auto_sync", _stub_maybe_auto_sync)
    return state


# ---------------------------------------------------------------------------
# removed top surfaces — Home starts at brief
# ---------------------------------------------------------------------------


def test_removed_top_surfaces_do_not_render(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "hv5-status" not in r.text
    assert "hv5-primary" not in r.text
    assert "hv5-clear" not in r.text
    assert "you're clear." not in r.text
    assert "handled" not in r.text
    assert "watching" not in r.text
    assert "hv5-tile" not in r.text


def test_home_section_order_is_brief_calendar_connected(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    brief_pos = r.text.index(">brief<")
    calendar_pos = r.text.index(">calendar<")
    connected_pos = r.text.index(">connected<")
    assert brief_pos < calendar_pos < connected_pos


def test_connected_section_uses_real_provider_state(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["instagram_connected"] = True
    stub_dashboard["google_gmail_connected"] = True
    stub_dashboard["google_calendar_connected"] = False
    r = client.get("/creator")
    assert r.status_code == 200
    assert ">connected<" in r.text
    assert 'data-slot="instagram"' in r.text
    assert 'data-slot="gmail"' in r.text
    assert 'data-slot="calendar"' in r.text
    assert 'aria-label="Instagram connected"' in r.text
    assert 'aria-label="Gmail connected"' in r.text
    assert 'aria-label="Calendar not connected"' in r.text
    assert 'href="/creator/instagram/dms"' in r.text
    assert 'href="/creator/dm"' in r.text
    assert 'href="/creator/profile/settings#integrations"' in r.text


def test_connected_section_marks_reconnect_state(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["instagram_connected"] = True
    stub_dashboard["instagram_needs_reconnect"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    assert "needs-attention" in r.text
    assert 'aria-label="Instagram needs reconnect"' in r.text


def test_native_dm_shows_as_babyg_not_instagram(
    client: TestClient, stub_dashboard, monkeypatch
) -> None:
    """A native unread DM does not create a Home manager card."""
    _signed_in(client)
    monkeypatch.setattr(
        home_briefing_module.dms, "list_threads_for_user",
        lambda uid: [
            {"id": "native-thread-1", "peer_id": "peer-1",
             "last_message_at": "2026-09-08T11:00:00Z",
             "participant_a_id": "u-1", "participant_b_id": "peer-1"},
        ],
    )
    monkeypatch.setattr(
        home_briefing_module.dms, "unread_counts_by_thread",
        lambda uid, ids: {"native-thread-1": 1},
    )
    monkeypatch.setattr(
        home_briefing_module.dms, "last_messages_by_thread",
        lambda ids: {"native-thread-1": {
            "body": "hey are you around",
            "created_at": "2026-09-08T11:00:00Z",
        }},
    )
    monkeypatch.setattr(
        home_briefing_module.profiles, "get_creator_profile",
        lambda uid: {"full_name": "Sam"},
    )
    r = client.get("/creator")
    assert r.status_code == 200
    assert "New message from Sam" not in r.text
    assert 'href="/creator/dm/native-thread-1"' not in r.text
    assert 'href="/creator/instagram/dms#thread-native-thread-1"' not in r.text


def test_bare_new_dm_notification_never_shows_as_instagram(
    client: TestClient, stub_dashboard
) -> None:
    """A legacy `new_dm` notification WITHOUT source_provider must not
    be labeled as Instagram in the carousel."""
    _signed_in(client)
    stub_dashboard["unread_notifs"] = [
        {
            "id": "legacy-dm",
            "kind": "new_dm",
            "title": "New message",
            "body": "hi",
            "link_path": "/creator/dm/legacy-thread",
            # No source_provider — the ambiguity case.
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    # The ambiguous notification is silently skipped.
    assert "New message" not in r.text
    assert 'data-source="instagram"' not in r.text


def test_instagram_brief_row_preserves_source_provider(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["brief_rows"] = [
        {
            "slot": "instagram",
            "title": "New message from @brand",
            "detail": "collab?",
            "href": "/creator/brief",
            "created_at": "2026-09-08T12:00:00Z",
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert 'data-slot="instagram"' in r.text
    assert "New message from @brand" in r.text
    assert 'href="/creator/brief"' in r.text


def test_home_visible_controls_have_real_hrefs(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert 'href="/creator/brief"' in r.text
    assert 'href="/creator/calendar"' in r.text
    assert 'href="/creator/profile/settings#integrations"' in r.text


# ---------------------------------------------------------------------------
# section 3: current week calendar
# ---------------------------------------------------------------------------


def test_next_shows_connect_calendar_when_disconnected(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert ">calendar<" in r.text
    assert "view calendar" in r.text
    assert "connect calendar" in r.text.lower()


def test_next_shows_first_booking_when_connected(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    today = date.today()
    stub_dashboard["bookings"] = [
        {
            "id": "b-1",
            "title": "Brand intro call",
            "starts_at": f"{today.isoformat()}T15:00:00Z",
            "ends_at": f"{today.isoformat()}T16:00:00Z",
            "venue_name": "Zoom",
        },
        {
            "id": "b-2",
            "title": "Studio shoot",
            "starts_at": f"{today.isoformat()}T18:00:00Z",
            "ends_at": f"{today.isoformat()}T19:00:00Z",
        },
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert "Brand intro call" in r.text
    assert 'href="/creator/calendar/b-1"' in r.text
    assert "Studio shoot" in r.text
    # The connect-calendar row must not show when connected.
    assert "connect calendar" not in r.text.lower()


def test_next_hides_row_when_connected_with_no_bookings(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    stub_dashboard["bookings"] = []
    r = client.get("/creator")
    assert r.status_code == 200
    # Section header + view-calendar still visible.
    assert ">calendar<" in r.text
    assert "view calendar" in r.text
    # No connect prompt, no invented event row.
    assert "connect calendar" not in r.text.lower()
    assert "nothing scheduled" in r.text


# ---------------------------------------------------------------------------
# section 4: brief — real signals only, max 3
# ---------------------------------------------------------------------------


def test_brief_is_hidden_when_no_real_signals(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # No fabricated brief rows when nothing real is available.
    assert ">brief<" in r.text
    assert 'class="hv5-brief-row"' not in r.text
    assert "nothing in your brief" in r.text


def test_brief_surfaces_real_brief_preview(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["ig_dm_unread"] = 3
    stub_dashboard["brief_rows"] = [
        {
            "slot": "instagram",
            "title": "@brand asked for rates",
            "detail": "clarify scope and usage",
            "href": "/creator/brief",
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert ">brief<" in r.text
    assert "@brand asked for rates" in r.text
    assert "3 unread instagram dms" not in r.text
    assert 'href="/creator/brief"' in r.text


def test_brief_connection_request_uses_babyg_icon_and_copy(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["brief_rows"] = [
        {
            "slot": "babyg",
            "title": "Someone wants to connect.",
            "detail": "A new creator or brand sent you a connection request.",
            "href": "/creator/brief",
            "created_at": "2026-09-08T12:00:00Z",
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert "Someone wants to connect on babyg" in r.text
    assert 'data-slot="babyg"' in r.text
    assert "logo-bg.png" in r.text


def test_brief_caps_at_three_rows(client: TestClient, stub_dashboard) -> None:
    _signed_in(client)
    stub_dashboard["brief_rows"] = [
        {"slot": "gmail", "title": "one", "detail": "a", "href": "/creator/brief"},
        {"slot": "instagram", "title": "two", "detail": "b", "href": "/creator/brief"},
        {"slot": "calendar", "title": "three", "detail": "c", "href": "/creator/brief"},
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert r.text.count('class="hv5-brief-row"') == 3


# ---------------------------------------------------------------------------
# no-legacy-surfaces regression checks
# ---------------------------------------------------------------------------


def test_home_does_not_render_top_wordmark_or_greeting(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # The v4 greeting header + 5-day strip must not appear on v5.
    assert "creator-home-hero" not in r.text
    assert 'aria-label="next five days"' not in r.text
    # No standalone social-analytics section / shortcuts grid.
    assert "creator-social-card" not in r.text
    assert "creator-home-shortcuts" not in r.text


def test_home_degrades_gracefully_when_bookings_service_errors(
    client: TestClient, stub_dashboard, monkeypatch
) -> None:
    """Bookings service raising must not blank the whole home page."""
    def _boom(*a, **kw):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(bookings_module, "list_for_user_range", _boom)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # Falls through to the disconnected calendar row.
    assert "connect calendar" in r.text.lower()


# ---------------------------------------------------------------------------
# calendar preview helper — still a pure function on the route module
# ---------------------------------------------------------------------------


def test_calendar_preview_days_are_real_and_consecutive() -> None:
    days = creator_routes._calendar_preview_days(date(2026, 6, 29))
    assert [(day["weekday"], day["day"]) for day in days] == [
        ("Mon", 29),
        ("Tue", 30),
        ("Wed", 1),
        ("Thu", 2),
        ("Fri", 3),
        ("Sat", 4),
        ("Sun", 5),
    ]
    assert days[0]["is_today"] is True
    assert days[0]["is_selected"] is True


# ---------------------------------------------------------------------------
# Tabbar — 5 destinations, calendar folded under Home
# ---------------------------------------------------------------------------


def test_tabbar_has_five_destinations_in_order(
    client: TestClient, stub_dashboard
) -> None:
    """Mobile tabbar order: Home → Discover → Babyg → DMs → Settings."""
    _signed_in(client)
    r = client.get("/creator")
    text = r.text
    positions = {
        "home": text.find('href="/creator"\n     data-tab="feed"'),
        "discover": text.find('href="/creator/discover"\n     data-tab="network"'),
        "babyg": text.find('href="/creator/bot"\n     data-tab="chat"'),
        "dms": text.find('href="/creator/dm"\n     data-tab="inbox"'),
        "settings": text.find('href="/creator/profile/settings"\n     data-tab="settings"'),
    }
    for label, pos in positions.items():
        assert pos != -1, f"{label} tab missing from rendered tabbar"
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    assert [k for k, _ in ordered] == ["home", "discover", "babyg", "dms", "settings"]
    assert 'data-tab="profile"' not in text


def test_tabbar_does_not_include_standalone_calendar_or_stats_tabs(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert 'data-tab="calendar"' not in r.text
    assert 'data-tab="stats"' not in r.text


def test_tabbar_marks_home_active_for_calendar_path(stub_dashboard) -> None:
    from unittest.mock import MagicMock

    from app.core.templating import templates

    request = MagicMock()
    request.url.path = "/creator/calendar"
    request.cookies = {}
    request.headers = {}
    rendered = templates.get_template(
        "_partials/creator_tabbar.html"
    ).render({"request": request})
    assert 'data-tab="feed"\n     class="active"' in rendered
    for not_active in ("chat", "inbox", "network", "settings"):
        assert f'data-tab="{not_active}"\n     class="active"' not in rendered


# ---------------------------------------------------------------------------
# Settings page unchanged
# ---------------------------------------------------------------------------


def test_settings_page_renders(client: TestClient, stub_dashboard) -> None:
    _signed_in(client)
    r = client.get("/creator/profile/settings")
    assert r.status_code == 200
    assert "settings" in r.text
    assert "edit profile" in r.text
    assert "deal preferences" in r.text


# ---------------------------------------------------------------------------
# home v5 — brief renders before calendar, and calendar is compact on mobile
# ---------------------------------------------------------------------------


def test_home_triggers_calendar_auto_sync_when_google_connected(
    client: TestClient, stub_dashboard
) -> None:
    """The Home render must call calendar_sync.maybe_auto_sync so a
    freshly-added Google Calendar event lands in bookings before the
    read fires. Without this, Home shows stale calendar data
    indefinitely (bug: real Friday event never appears)."""
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    assert stub_dashboard["auto_sync_calls"] == ["u-1"]


def test_home_skips_calendar_auto_sync_when_google_not_connected(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert stub_dashboard["auto_sync_calls"] == []


def test_home_brief_renders_before_calendar(
    client: TestClient, stub_dashboard
) -> None:
    """The mandated home v5 order is:
       status -> primary -> brief -> calendar -> handled/watching.
    Locks brief above calendar so a future refactor cannot swap
    them silently."""
    _signed_in(client)
    stub_dashboard["brief_rows"] = [
        {
            "slot": "instagram",
            "title": "@brand asked for rates",
            "detail": "clarify scope and usage",
            "href": "/creator/brief",
        }
    ]
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    brief_pos = r.text.find(">brief<")
    calendar_pos = r.text.find('data-home-calendar')
    assert brief_pos != -1
    assert calendar_pos != -1
    assert brief_pos < calendar_pos


def test_home_mobile_calendar_omits_hourly_grid_markup_footprint(
    client: TestClient, stub_dashboard
) -> None:
    """Mobile Home must not render the giant 6am-10pm hourly timeline
    inside the compact list block. The `.creator-home-day-list`
    container is the mobile canonical event surface."""
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    # The compact week event list block is present.
    assert 'creator-home-day-events' in r.text
    assert 'creator-home-day-list' in r.text
    # The old hourly grid does not render on Home at any viewport.
    assert "calendar-home-grid" not in r.text
    assert "calendar-week-head" not in r.text
    assert 'data-home-calendar' in r.text


def test_home_mobile_calendar_renders_all_seven_day_cells(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    # Each of the seven days links to its real Calendar destination.
    day_cell_count = r.text.count('/creator/calendar?view=day&date=')
    assert day_cell_count == 7


def test_home_mobile_calendar_shows_nothing_scheduled_when_empty(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    # No bookings at all.
    stub_dashboard["bookings"] = []
    r = client.get("/creator")
    assert r.status_code == 200
    # `nothing scheduled` appears at least once (empty-day placeholder).
    assert "nothing scheduled" in r.text


def test_home_mobile_calendar_all_day_event_shows_all_day_label(
    client: TestClient, stub_dashboard
) -> None:
    """Real all-day events render with the `ALL DAY` prefix per spec."""
    from datetime import date
    from datetime import timedelta as _td
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    # Anchor to today's date in the fixture so the event lands inside
    # the current week window.
    today = date.today()
    stub_dashboard["bookings"] = [
        {
            "id": "b-1",
            "title": "shoot day",
            # ISO-only (no `T`) triggers the all-day code path in
            # _event_vm's date-only branch.
            "starts_at": today.isoformat(),
            "ends_at": (today + _td(days=1)).isoformat(),
            "type": "event",
            "status": "confirmed",
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert "all day" in r.text
    assert "shoot day" in r.text


# ---------------------------------------------------------------------------
# Home week-strip explicit navigation contract
# ---------------------------------------------------------------------------


def test_home_calendar_no_longer_loads_day_picker_js(
    client: TestClient, stub_dashboard
) -> None:
    """Home day cells navigate explicitly to Calendar instead of relying
    on a separate in-page picker script."""
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    assert "/static/js/creator_home_calendar.js" not in r.text


def test_home_week_strip_uses_real_calendar_links(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    body = r.text
    assert 'data-home-day-strip' not in body
    assert 'data-home-day-events' not in body
    assert body.count('/creator/calendar?view=day&date=') == 7


# ---------------------------------------------------------------------------
# Full Calendar mobile CSS contract
# ---------------------------------------------------------------------------


def _css() -> str:
    return APP_CSS.read_text()


def test_full_calendar_month_only_header_and_nav_present() -> None:
    """/creator/calendar is month-only. Header is a two-row block:
    ROW 1 = calendar eyebrow + google synced pill (when connected),
    ROW 2 = month title + previous/today/next nav. No `add item`
    button in the header; no view selector (day/week/month) anywhere."""
    css = _css()
    assert ".calendar-month-only-page .calendar-month-header {" in css
    assert ".calendar-month-only-page .calendar-month-header-row {" in css
    assert ".calendar-month-only-page .calendar-month-title {" in css
    assert ".calendar-month-only-page .calendar-month-nav {" in css
    assert ".calendar-month-only-page .calendar-month-nav-btn {" in css


def test_full_calendar_month_only_today_dot_and_selected_state() -> None:
    """Today marker is a small filled pink circle BEHIND the date number
    only — never a rectangle or full-cell tint. Selected state is a
    distinct treatment on the whole cell so today + selected can
    coexist visually."""
    css = _css()
    assert ".calendar-month-only-page .calendar-today-dot {" in css
    assert ".calendar-month-only-page .calendar-month-cell.is-selected {" in css
    # Today dot lives inside the date span, absolutely positioned so
    # the date number sits on top.
    dot_block = css.split(".calendar-month-only-page .calendar-today-dot {", 1)[1]
    dot_block = dot_block.split("}", 1)[0]
    assert "position: absolute" in dot_block
    assert "border-radius: 50%" in dot_block


def test_full_calendar_month_only_event_overflow_chip() -> None:
    """Cells show a maximum of 2 event previews plus a `+N` overflow
    chip so cell heights stay consistent."""
    css = _css()
    assert ".calendar-month-only-page .calendar-month-cell-event {" in css
    assert ".calendar-month-only-page .calendar-month-cell-more {" in css


def test_full_calendar_month_only_bottom_sheet_styles_present() -> None:
    """Tapping a day opens a bottom sheet — never a browser modal or
    a new page. The sheet has a backdrop, a panel anchored to the
    bottom, a title, an event list, and a `+ add` button."""
    css = _css()
    assert ".calendar-month-only-page .calendar-sheet {" in css
    assert ".calendar-month-only-page .calendar-sheet-backdrop {" in css
    assert ".calendar-month-only-page .calendar-sheet-panel {" in css
    assert ".calendar-month-only-page .calendar-sheet-events {" in css
    assert ".calendar-month-only-page .calendar-sheet-add {" in css
    assert ".calendar-month-only-page .calendar-sheet-form {" in css
