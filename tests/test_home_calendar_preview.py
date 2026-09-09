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

from datetime import UTC, date, datetime, timedelta
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
    agent_cycles as agent_cycles_module,
)
from app.services import (
    agent_recap as agent_recap_module,
)
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
    home_manager as home_manager_module,
)
from app.services import (
    instagram_dms as instagram_dms_module,
)
from app.services import (
    instagram_metrics as instagram_metrics_module,
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
        "matched_picks": [],
        "pending_actions": [],
        "pending_connections": [],
        "unread_dms": 0,
        "unread_ig_dms": 0,
        "manager_activity": [],
        "overnight_recap": None,
        "instagram_connection": None,
        "instagram_snapshot": None,
        "instagram_growth": {},
        "latest_agent_cycle": None,
        "latest_sweep": None,
        "open_deals": 0,
    }
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: state["profile"]
    )
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=8: [])
    monkeypatch.setattr(
        notifications_module,
        "list_manager_activity",
        lambda uid, *, limit=4: list(state["manager_activity"]),
    )
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(
        dms_module, "unread_count_for_user", lambda uid: state["unread_dms"]
    )
    monkeypatch.setattr(
        instagram_dms_module,
        "unread_count_for_creator",
        lambda uid: state["unread_ig_dms"],
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
        agent_recap_module,
        "build",
        lambda uid: state["overnight_recap"],
    )
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: list(state["bookings"]),
    )
    monkeypatch.setattr(
        oauth_module, "get_google_connection",
        lambda uid: {"connected": True} if state["calendar_connected"] else None,
    )
    monkeypatch.setattr(
        oauth_module,
        "get_instagram_connection",
        lambda uid: state["instagram_connection"],
    )
    monkeypatch.setattr(
        oauth_module, "google_calendar_connected",
        lambda conn: bool(conn),
    )
    monkeypatch.setattr(
        oauth_module,
        "google_gmail_connected",
        lambda conn: bool(conn and state.get("gmail_connected")),
    )
    monkeypatch.setattr(
        instagram_metrics_module,
        "latest_snapshot",
        lambda uid: state["instagram_snapshot"],
    )
    monkeypatch.setattr(
        instagram_metrics_module,
        "growth_over",
        lambda uid, **kw: state["instagram_growth"],
    )
    monkeypatch.setattr(
        agent_cycles_module,
        "latest",
        lambda uid: state["latest_agent_cycle"],
    )
    monkeypatch.setattr(
        home_manager_module,
        "latest_sweep_run",
        lambda uid: state["latest_sweep"],
    )
    monkeypatch.setattr(
        home_manager_module,
        "open_deal_count",
        lambda uid: state["open_deals"],
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
    assert "calls and deadlines" in r.text.lower()
    # "open calendar" link is always present so the user can dig in.
    assert "/creator/calendar" in r.text
    assert 'aria-label="next five days"' not in r.text
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
                "starts_at": (datetime.now(UTC) + timedelta(hours=3)).isoformat(),
                "venue_name": "Zoom",
            },
            {
                "id": "b-2",
                "title": "Studio shoot",
                "starts_at": (datetime.now(UTC) + timedelta(hours=6)).isoformat(),
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
    assert "you're clear" in r.text.lower()
    assert "connect calendar" not in r.text.lower()


def test_home_v2_does_not_render_shortcut_grid_when_nothing_needs_attention(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "creator-home-shortcuts" not in r.text
    assert "social analytics" not in r.text.lower()
    assert "nothing needs your attention right now" in r.text.lower()


def test_home_surfaces_manager_activity_with_deep_link(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["manager_activity"] = [
        {
            "id": "n-1",
            "kind": "new_dm",
            "title": "new instagram message from @brandco",
                "body": "paid collab rates? I can draft a reply.",
                "link_path": "/creator/instagram/dms?thread=ig-thread-1#ig-thread-ig-thread-1",
                "priority": "high",
                "source_provider": "instagram",
                "created_at": "2026-09-08T10:00:00Z",
            }
        ]

    r = client.get("/creator")

    assert r.status_code == 200
    assert "needs you" in r.text
    assert "you're clear" not in r.text.lower()
    assert "new instagram message from @brandco" in r.text
    assert (
        "/creator/instagram/dms?thread=ig-thread-1#ig-thread-ig-thread-1"
        in r.text
    )
    assert "review" in r.text


def test_instagram_dm_deep_link_marks_thread_and_notification_read(
    client: TestClient, stub_dashboard, monkeypatch
) -> None:
    _signed_in(client)
    calls: list[tuple[str, dict[str, str]]] = []

    monkeypatch.setattr(
        instagram_dms_module,
        "mark_thread_read_for_creator",
        lambda **kw: calls.append(("ig", kw)) or True,
    )
    monkeypatch.setattr(
        notifications_module,
        "mark_thread_read",
        lambda **kw: calls.append(("notification", kw)) or 1,
    )
    monkeypatch.setattr(
        instagram_dms_module,
        "list_threads_for_creator",
        lambda uid, *, limit=30: [
            {
                "id": "thread-1",
                "ig_thread_id": "peer-1",
                "ig_peer_user_id": "peer-1",
                "peer_username": "brandco",
                "last_message_at": "2026-09-08T10:00:00Z",
                "unread_count": 0,
            }
        ],
    )
    monkeypatch.setattr(
        instagram_dms_module,
        "list_messages_for_thread",
        lambda uid, thread_id, *, limit=40: [
            {
                "id": "msg-1",
                "thread_id": thread_id,
                "direction": "inbound",
                "body": "paid collab rates?",
                "received_at": "2026-09-08T10:00:00Z",
            }
        ],
    )

    r = client.get("/creator/instagram/dms?thread=thread-1")

    assert r.status_code == 200
    assert ("ig", {"user_id": "u-1", "thread_id": "thread-1"}) in calls
    assert (
        "notification",
        {
            "user_id": "u-1",
            "source_provider": "instagram",
            "thread_id": "thread-1",
        },
    ) in calls
    assert 'id="ig-thread-thread-1"' in r.text
    assert "opened" in r.text


def test_home_needs_you_ranks_real_pending_state(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["unread_dms"] = 3
    stub_dashboard["pending_connections"] = [
        {"id": "c-1", "requester_id": "u-2"},
        {"id": "c-2", "requester_id": "u-3"},
    ]
    stub_dashboard["matched_picks"] = [
        {
            "card_id": "op-1",
            "card_kind": "opportunity",
            "title": "Rooftop shoot",
        }
    ]

    r = client.get("/creator")

    assert r.status_code == 200
    assert "needs you" in r.text
    assert "wants to connect" in r.text
    assert "BabyG's brief" in r.text
    assert "Rooftop shoot" in r.text
    assert (
        'href="/creator/discover?bring_back_kind=opportunity&amp;bring_back_id=op-1"'
        in r.text
    )


def test_home_status_shows_real_disconnected_integrations(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert 'href="/creator/instagram/connect?next=/creator"' in r.text
    assert "message monitoring unavailable" in r.text.lower()
    assert "gmail and calendar not connected" in r.text.lower()


def test_home_ignores_social_platform_query_without_dead_links(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator?social_platform=tiktok")
    assert r.status_code == 200
    assert "/creator/tiktok/connect" not in r.text
    assert "social analytics" not in r.text.lower()

    r = client.get("/creator?social_platform=youtube")
    assert r.status_code == 200
    assert "/creator/youtube/connect" not in r.text
    assert "social analytics" not in r.text.lower()


def test_home_renders_stored_instagram_growth_as_brief_not_live_analytics(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["instagram_connection"] = {"provider_account_id": "ig-1"}
    stub_dashboard["instagram_growth"] = {"followers_count": 42}
    r = client.get("/creator")
    assert r.status_code == 200
    assert "BabyG's brief" in r.text
    assert "Instagram followers are up" in r.text
    assert "+42 over the latest stored 7-day window" in r.text
    assert "top post signal" not in r.text
    assert "social analytics" not in r.text.lower()


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
                "starts_at": (datetime.now(UTC) + timedelta(hours=i + 1)).isoformat(),
                "venue_name": None,
            }
        for i in range(8)
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    # Home V2 shows only the first two relevant calendar rows. The full
    # list remains on /creator/calendar.
    for i in range(2):
        assert f"Event {i}" in r.text
    for i in range(2, 8):
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
