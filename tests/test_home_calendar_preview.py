"""Home calendar preview + tab consolidation.

Lock in the nav/content reorganization:
  * Mobile tabbar has 5 tabs (Home / Discover / Babyg / DMs / Profile)
    with Calendar removed and folded under Home.
  * Dashboard at /creator surfaces a compact upcoming-events preview
    and a clear empty state when calendar isn't connected.
  * The /creator/calendar route still exists; visiting it leaves the
    Home tab marked active (not its own tab).
  * The /creator/performance route is reachable from the Profile page
    so the deprecated Stats tab doesn't strand the surface.

Service calls are stubbed so tests never hit Supabase.
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes
from app.services import (
    bookings as bookings_module,
)
from app.services import (
    discover as discover_module,
)
from app.services import (
    dms as dms_module,
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
    """Lean defaults that let /creator render without hitting Supabase."""
    state: dict[str, Any] = {
        "profile": _profile(),
        "bookings": [],
        "calendar_connected": False,
        "performance_view": stats_merge_module.PerformanceView(
            rows=[],
            instagram_status=stats_merge_module.IG_STATUS_NOT_CONNECTED,
        ),
    }
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: state["profile"]
    )
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=4: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(network_module, "list_incoming_pending", lambda uid: [])
    monkeypatch.setattr(discover_module, "list_cards", lambda **kw: [])
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: list(state["bookings"]),
    )
    monkeypatch.setattr(
        oauth_module, "get_google_connection",
        lambda uid: {"connected": True} if state["calendar_connected"] else None,
    )
    monkeypatch.setattr(
        oauth_module, "google_calendar_connected",
        lambda conn: bool(conn),
    )
    monkeypatch.setattr(
        stats_merge_module,
        "performance_view",
        lambda uid, **kw: state["performance_view"],
    )
    return state


# ---------------------------------------------------------------------------
# /creator (Home) — calendar preview surface
# ---------------------------------------------------------------------------


def test_home_renders_today_section_when_calendar_not_connected(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # Empty state copy is the honest "connect to see" prompt the user
    # spec mandates — no fake events, no fake "ai" upsell.
    assert "connect calendar" in r.text.lower()
    assert "today's calls" in r.text.lower()
    # "open calendar" link is always present so the user can dig in.
    assert "/creator/calendar" in r.text
    assert 'aria-label="next five days"' in r.text
    assert 'aria-current="date"' in r.text
    assert "<strong>now</strong>" not in r.text


def test_calendar_preview_days_are_real_and_consecutive() -> None:
    days = creator_routes._calendar_preview_days(date(2026, 6, 29))
    assert days == [
        {"weekday": "Mon", "day": 29, "is_today": True},
        {"weekday": "Tue", "day": 30, "is_today": False},
        {"weekday": "Wed", "day": 1, "is_today": False},
        {"weekday": "Thu", "day": 2, "is_today": False},
        {"weekday": "Fri", "day": 3, "is_today": False},
    ]


def test_home_renders_upcoming_events_when_present(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["calendar_connected"] = True
    stub_dashboard["bookings"] = [
        {
            "id": "b-1",
            "title": "Brand intro call",
            "starts_at": "2026-06-19T15:00:00Z",
            "venue_name": "Zoom",
        },
        {
            "id": "b-2",
            "title": "Studio shoot",
            "starts_at": "2026-06-20T18:00:00Z",
            "venue_name": None,
        },
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert "Brand intro call" in r.text
    assert "Studio shoot" in r.text
    assert 'href="/creator/calendar/b-1"' in r.text
    # Section is now titled "today" — the section header is on the page.
    assert ">today<" in r.text
    # The connect-calendar prompt must not also appear once connected.
    assert "connect calendar" not in r.text.lower()


def test_home_renders_quiet_empty_state_when_connected_but_no_events(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["calendar_connected"] = True
    stub_dashboard["bookings"] = []
    r = client.get("/creator")
    assert r.status_code == 200
    assert "nothing on the books" in r.text.lower()
    assert "connect calendar" not in r.text.lower()


def test_home_renders_social_analytics_connect_state(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "social analytics" in r.text
    assert "connect instagram for live signals" in r.text.lower()
    assert 'href="/creator/performance"' in r.text
    assert "top post signal" not in r.text


def test_home_renders_social_analytics_from_real_instagram_rows(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["performance_view"] = stats_merge_module.PerformanceView(
        rows=[
            stats_merge_module.StatsRow(
                source=stats_merge_module.SOURCE_INSTAGRAM,
                title="Empire Social Lounge",
                timestamp="2026-06-20T12:00:00+0000",
                permalink="https://www.instagram.com/p/one",
                metrics={
                    "likes": 88,
                    "comments": 7,
                    "reach": 1200,
                    "engagement": 140,
                    "saved": 19,
                },
                notes=None,
            ),
            stats_merge_module.StatsRow(
                source=stats_merge_module.SOURCE_INSTAGRAM,
                title="Swan dinner recap",
                timestamp="2026-06-18T12:00:00+0000",
                permalink="https://www.instagram.com/p/two",
                metrics={
                    "likes": 40,
                    "comments": 3,
                    "reach": 500,
                    "engagement": 62,
                    "saved": 8,
                },
                notes=None,
            ),
        ],
        instagram_status=stats_merge_module.IG_STATUS_OK,
    )
    r = client.get("/creator")
    assert r.status_code == 200
    assert "live instagram" in r.text
    assert "top post signal" in r.text
    assert "Empire Social Lounge" in r.text
    assert "1.7k" in r.text
    assert "saves" in r.text
    assert "comments" in r.text


def test_home_caps_upcoming_preview_to_three_items(
    client: TestClient, stub_dashboard
) -> None:
    """Home stays uncluttered. Preview shows only the next few — full
    list is on /creator/calendar."""
    _signed_in(client)
    stub_dashboard["calendar_connected"] = True
    stub_dashboard["bookings"] = [
        {
            "id": f"b-{i}",
            "title": f"Event {i}",
            "starts_at": f"2026-06-{20+i:02d}T10:00:00Z",
            "venue_name": None,
        }
        for i in range(8)
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    # Event 0..2 visible; Event 3..7 must not be rendered.
    for i in range(3):
        assert f"Event {i}" in r.text
    for i in range(3, 8):
        assert f"Event {i}" not in r.text


def test_home_degrades_gracefully_when_bookings_service_errors(
    client: TestClient, stub_dashboard, monkeypatch
) -> None:
    """A flaky Supabase must not blank the dashboard. The dashboard
    route wraps the lookup in try/except so it falls through to the
    no-events empty state."""
    def _boom(*a, **kw):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(bookings_module, "list_for_user", _boom)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # Empty-state copy still renders (calendar disconnected default).
    assert "connect calendar" in r.text.lower()


# ---------------------------------------------------------------------------
# Tabbar / sidebar — 5 destinations, calendar folded under Home
# ---------------------------------------------------------------------------


def test_tabbar_has_five_destinations_in_order(
    client: TestClient, stub_dashboard
) -> None:
    """Mobile tabbar order: Home → Discover → Babyg → DMs → Settings.
    Profile was absorbed into Settings as a disclosure section, so the
    dedicated Profile tab was removed."""
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
    # Every destination is present on the page.
    for label, pos in positions.items():
        assert pos != -1, f"{label} tab missing from rendered tabbar"
    # Order is left-to-right as specified.
    ordered = sorted(positions.items(), key=lambda kv: kv[1])
    assert [k for k, _ in ordered] == ["home", "discover", "babyg", "dms", "settings"]
    # Profile tab is gone — settings owns it now.
    assert 'data-tab="profile"' not in text


def test_tabbar_does_not_include_standalone_calendar_or_stats_tabs(
    client: TestClient, stub_dashboard
) -> None:
    """Calendar moves to Home; Stats moves to Settings. Neither should
    appear as a top-level tab."""
    _signed_in(client)
    r = client.get("/creator")
    assert 'data-tab="calendar"' not in r.text
    assert 'data-tab="stats"' not in r.text


def test_tabbar_marks_home_active_for_calendar_path(stub_dashboard) -> None:
    """The tabbar partial marks Home active whenever the current path
    starts with /creator/calendar. Render the partial directly with a
    synthetic request so we don't depend on the full /creator/calendar
    route's service plumbing."""
    from unittest.mock import MagicMock

    from app.core.templating import templates

    request = MagicMock()
    request.url.path = "/creator/calendar"
    request.cookies = {}
    request.headers = {}
    rendered = templates.get_template(
        "_partials/creator_tabbar.html"
    ).render({"request": request})
    # Home anchor is active when path starts with /creator/calendar.
    assert 'data-tab="feed"\n     class="active"' in rendered
    # The chat / inbox / network / settings tabs must NOT be active.
    for not_active in ("chat", "inbox", "network", "settings"):
        assert f'data-tab="{not_active}"\n     class="active"' not in rendered


# ---------------------------------------------------------------------------
# Settings page renders — profile card + editor now live at the top of
# settings; the standalone "insights & receipts" shortcut card was cut
# to keep the surface actual controls only. /creator/performance and
# /creator/receipts remain routable, just not surfaced from settings.
# ---------------------------------------------------------------------------


def test_settings_page_renders(client: TestClient, stub_dashboard) -> None:
    _signed_in(client)
    r = client.get("/creator/profile/settings")
    assert r.status_code == 200
    assert "settings" in r.text
    # The profile card + editor was merged in as the top section.
    assert "edit profile" in r.text
    assert "deal preferences" in r.text
