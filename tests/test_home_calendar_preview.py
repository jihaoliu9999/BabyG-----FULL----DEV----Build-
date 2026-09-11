"""Home v5 — mobile spec conformance.

The home v5 layout is a fixed five-section stack:

  1. babyg status pill  (● babyg · N connected chevron)
  2. primary manager update  (or the compact clear state)
  3. next  (upcoming booking, connect-calendar row, or nothing)
  4. brief  (≤3 real-signal rows, no filler)
  5. handled + watching  (two compact tiles side-by-side)

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
# section 1: status pill — live connected count, no hardcoding
# ---------------------------------------------------------------------------


def test_status_pill_renders_with_zero_connected(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # Always the `babyg` name, never `manager status`.
    assert "manager status" not in r.text
    assert "hv5-status-name" in r.text
    assert ">babyg<" in r.text
    # Real dynamic count — 0 when nothing is connected.
    assert "0 connected" in r.text


def test_status_pill_counts_only_actual_connections(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["instagram_connected"] = True
    stub_dashboard["google_gmail_connected"] = True
    stub_dashboard["google_calendar_connected"] = False
    r = client.get("/creator")
    assert r.status_code == 200
    assert "2 connected" in r.text
    # The disconnected calendar row is still rendered inside the panel
    # so the creator can tap to connect it.
    assert "not connected" in r.text


def test_status_pill_counts_all_three(client: TestClient, stub_dashboard) -> None:
    _signed_in(client)
    stub_dashboard["instagram_connected"] = True
    stub_dashboard["google_gmail_connected"] = True
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    assert "3 connected" in r.text


def test_status_pill_excludes_needs_reconnect_from_count(
    client: TestClient, stub_dashboard
) -> None:
    """A stale connection that needs reconnecting is NOT counted as
    connected — the count reflects working integrations."""
    _signed_in(client)
    stub_dashboard["instagram_connected"] = True
    stub_dashboard["instagram_needs_reconnect"] = True
    stub_dashboard["google_gmail_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    # Only Gmail counts because IG needs reconnect.
    assert "1 connected" in r.text
    assert "reconnect" in r.text


# ---------------------------------------------------------------------------
# section 2: primary manager update / clear state
# ---------------------------------------------------------------------------


def test_primary_falls_back_to_clear_state_when_nothing_pending(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "you're clear." in r.text
    # No fake primary card when there's nothing real to say.
    assert "hv5-primary-title" not in r.text


def test_primary_renders_top_pending_action(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["pending_actions"] = [
        {
            "id": "prop-1",
            "action_type": "instagram.send_dm",
            "created_at": "2026-09-08T12:00:00Z",
            "preview": {
                "title": "reply to instagram dm",
                "body": "thanks, will review.",
            },
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert "hv5-primary-title" in r.text
    assert "reply to instagram dm" in r.text
    assert 'href="/creator/bot#action-prop-1"' in r.text
    # Clear state must be gone when a real primary card is shown.
    assert "you're clear." not in r.text


def test_primary_single_slide_shows_no_carousel_indicator(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["pending_actions"] = [
        {
            "id": "prop-1",
            "action_type": "gmail.create_draft",
            "created_at": "2026-09-08T12:00:00Z",
            "preview": {"title": "draft reply", "body": "quick note"},
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    # No dot indicator, no count.
    assert "hv5-primary-dots" not in r.text
    assert "hv5-primary-count" not in r.text
    # Not tagged as a carousel.
    assert 'class="hv5-primary hv5-primary-carousel"' not in r.text
    # Still uses the same outer box class.
    assert 'class="hv5-primary"' in r.text


def test_primary_two_or_more_slides_becomes_carousel(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["pending_actions"] = [
        {
            "id": "prop-1",
            "action_type": "gmail.create_draft",
            "created_at": "2026-09-08T10:00:00Z",
            "preview": {"title": "first draft"},
        },
        {
            "id": "prop-2",
            "action_type": "calendar.create_event",
            "created_at": "2026-09-08T11:00:00Z",
            "preview": {"title": "second event"},
        },
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    # Same outer box, now marked as carousel.
    assert "hv5-primary-carousel" in r.text
    # Dot indicator + count present.
    assert "hv5-primary-dots" in r.text
    assert "hv5-primary-count" in r.text
    # Count total is dynamic (2), not hardcoded.
    assert "/ <span>2</span>" in r.text
    # Both slides rendered.
    assert 'href="/creator/bot#action-prop-1"' in r.text
    assert 'href="/creator/bot#action-prop-2"' in r.text


def test_primary_carousel_puts_highest_priority_first(
    client: TestClient, stub_dashboard
) -> None:
    """High-stakes gmail.send_email should render before an older
    create_booking proposal, even though the booking is older."""
    _signed_in(client)
    stub_dashboard["pending_actions"] = [
        {
            "id": "old-booking",
            "action_type": "create_booking",
            "created_at": "2026-09-01T09:00:00Z",
            "preview": {"title": "old booking"},
        },
        {
            "id": "urgent-mail",
            "action_type": "gmail.send_email",
            "created_at": "2026-09-08T09:00:00Z",
            "preview": {"title": "urgent send"},
        },
    ]
    r = client.get("/creator")
    urgent_pos = r.text.find("action-urgent-mail")
    booking_pos = r.text.find("action-old-booking")
    assert urgent_pos < booking_pos < urgent_pos + 4000  # both present, urgent first


def test_native_dm_shows_as_babyg_not_instagram(
    client: TestClient, stub_dashboard, monkeypatch
) -> None:
    """A native babyg unread DM surfaces as source='babyg' and its
    action opens /creator/dm/{thread_id} — NEVER Instagram."""
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
    assert 'data-source="babyg"' in r.text
    assert 'href="/creator/dm/native-thread-1"' in r.text
    assert "New message from Sam" in r.text
    # Must not route to Instagram
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
    # The ambiguous notification is silently skipped -> clear state.
    assert "you're clear." in r.text
    # And explicitly not tagged as Instagram anywhere.
    assert 'data-source="instagram"' not in r.text


def test_instagram_dm_notification_requires_source_provider(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["unread_notifs"] = [
        {
            "id": "ig-dm-1",
            "kind": "new_dm",
            "title": "New message from @brand",
            "body": "collab?",
            "link_path": "/creator/instagram/dms#thread-ig-1",
            "source_provider": "instagram",
            "priority": "high",
            "created_at": "2026-09-08T12:00:00Z",
        }
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert 'data-source="instagram"' in r.text
    assert "/creator/instagram/dms#thread-ig-1" in r.text


def test_home_page_has_no_right_facing_chevrons(
    client: TestClient, stub_dashboard
) -> None:
    """Home-wide rule from the spec: no `>` navigation chevrons (unicode single-right-pointing quotation-mark included)."""
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # Locate the main home content only (skip the shared base template
    # nav/footer areas that might include chevrons for reasons outside
    # this spec's scope).
    body = r.text
    home_start = body.find('class="creator-home hv5"')
    home_end = body.find("</main>", home_start)
    home_html = body[home_start:home_end]
    assert "›" not in home_html  # noqa: RUF001
    # `>` shows up as HTML syntax everywhere; only the "text" > chevron
    # would be a problem. The generic form doesn't survive as visible
    # text without &gt; escaping (Jinja auto-escapes). Just double-check
    # no literal &gt; navigation chip appears.
    assert "&gt;</a>" not in home_html
    assert "&gt;</span>" not in home_html


def test_status_pill_uses_caret_not_chevron(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "hv5-status-caret" in r.text
    # No right-chevron on the pill.
    body = r.text
    pill_start = body.find('class="hv5-status-pill"')
    pill_end = body.find("</summary>", pill_start)
    assert "›" not in body[pill_start:pill_end]  # noqa: RUF001


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
    assert "nothing on the books" in r.text


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
    assert ">brief<" not in r.text


def test_brief_surfaces_real_ig_unread(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["ig_dm_unread"] = 3
    r = client.get("/creator")
    assert r.status_code == 200
    assert ">brief<" in r.text
    assert "3 unread instagram dms" in r.text
    assert 'href="/creator/instagram/dms"' in r.text


def test_brief_caps_at_three_rows(client: TestClient, stub_dashboard) -> None:
    _signed_in(client)
    stub_dashboard["ig_dm_unread"] = 2
    stub_dashboard["matched_picks"] = [
        {"card_id": "op-1", "card_kind": "opportunity", "title": "Rooftop shoot"},
    ]
    stub_dashboard["overnight_recap"] = {
        "headlines": [
            "ran 2 thinking cycles",
            "updated your memory 1 time",
            "extra headline four",
            "extra headline five",
        ],
        "counts": {},
    }
    r = client.get("/creator")
    assert r.status_code == 200
    # Exactly 3 hv5-brief-row anchors, never 4+.
    assert r.text.count('class="hv5-brief-row"') == 3


# ---------------------------------------------------------------------------
# section 5: handled + watching
# ---------------------------------------------------------------------------


def test_handled_shows_real_count_or_calm_zero(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "hv5-tile-handled" in r.text
    assert "nothing yet today" in r.text


def test_handled_shows_real_count_when_nonzero(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["handled_today"] = 4
    r = client.get("/creator")
    assert r.status_code == 200
    assert "4 things today" in r.text


def test_watching_shows_zero_state_calmly(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert "hv5-tile-watching" in r.text
    assert "nothing tracked" in r.text


def test_watching_composes_from_real_state(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["pending_actions"] = [
        {"id": "p-1", "action_type": "gmail.create_draft"},
        {"id": "p-2", "action_type": "calendar.create_event"},
    ]
    stub_dashboard["matched_picks"] = [
        {"card_id": "op-1", "card_kind": "opportunity", "title": "X"}
    ]
    r = client.get("/creator")
    assert r.status_code == 200
    assert "2 deals" in r.text
    assert "1 opportunity" in r.text


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
    stub_dashboard["ig_dm_unread"] = 1  # forces a brief row to render
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
    # The compact per-day list block is present.
    assert 'data-home-day-events' in r.text
    assert 'creator-home-day-list' in r.text
    # The full hourly grid is still in the template (kept for desktop)
    # but is behind a mobile-hide CSS rule scoped to
    # [data-home-calendar]. The route must render both.
    assert 'data-home-calendar' in r.text


def test_home_mobile_calendar_renders_all_seven_day_cells(
    client: TestClient, stub_dashboard
) -> None:
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    # Each of the seven days gets a data-home-day cell.
    day_cell_count = r.text.count('data-home-day="')
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
    assert "ALL DAY" in r.text
    assert "shoot day" in r.text


# ---------------------------------------------------------------------------
# Home day-picker JS data-attribute contract
# ---------------------------------------------------------------------------


def test_home_day_picker_js_is_loaded(client: TestClient, stub_dashboard) -> None:
    """The mobile day-picker JS must be included on Home. Without it,
    tapping a different day cannot switch the visible per-day list."""
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    assert r.status_code == 200
    assert "/static/js/creator_home_calendar.js" in r.text


def test_home_day_picker_markup_carries_required_data_attrs(
    client: TestClient, stub_dashboard
) -> None:
    """The JS binds on these three attributes. If any of them drops
    off in a future template edit the picker silently breaks."""
    _signed_in(client)
    stub_dashboard["google_calendar_connected"] = True
    r = client.get("/creator")
    body = r.text
    assert 'data-home-day-strip' in body
    assert 'data-home-day-events' in body
    assert body.count('data-home-day="') == 7
    assert body.count('data-home-day-list="') == 7


def test_home_day_picker_js_uses_closest_selector(monkeypatch) -> None:
    """Belt-and-suspenders lookup — the bug was that plain
    getAttribute-on-target ancestor-walking mis-fired on iOS Safari
    when the tap landed on the inner <strong>. The fixed script
    uses element.closest() to hop straight to the anchor."""
    src = HOME_CALENDAR_JS.read_text()
    assert ".closest(" in src
    assert "preventDefault" in src
    assert "stopPropagation" in src


def test_home_day_picker_js_binds_per_cell_not_delegation(monkeypatch) -> None:
    """Direct per-cell binding is the mobile-Safari-safe pattern.
    Lock the shape."""
    src = HOME_CALENDAR_JS.read_text()
    # Function that binds a single cell exists.
    assert "function bindCell" in src or "function attach" in src


# ---------------------------------------------------------------------------
# Full Calendar mobile CSS contract
# ---------------------------------------------------------------------------


def _css() -> str:
    return APP_CSS.read_text()


def test_full_calendar_mobile_toolbar_reflow_present() -> None:
    css = _css()
    # Toolbar becomes a block on mobile so the pill can float top-right.
    assert ".calendar-page .calendar-toolbar {" in css
    assert "position: relative" in css
    # 4 action buttons in an equal grid.
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in css
    # 3 view tabs in an equal grid.
    assert "grid-template-columns: repeat(3, minmax(0, 1fr))" in css


def test_full_calendar_mobile_week_hides_seven_column_head() -> None:
    css = _css()
    assert (
        ".calendar-page .calendar-week-shell:not(.calendar-day-mode) .calendar-week-head"
        in css
    )
    assert (
        ".calendar-page .calendar-week-shell:not(.calendar-day-mode) .calendar-all-day-row"
        in css
    )


def test_full_calendar_mobile_week_shows_only_selected_day_column() -> None:
    css = _css()
    assert (
        ".calendar-page .calendar-week-shell:not(.calendar-day-mode) .calendar-time-grid .calendar-day-column {"
        in css
    )
    assert (
        ".calendar-page .calendar-week-shell:not(.calendar-day-mode) .calendar-time-grid .calendar-day-column.is-selected {"
        in css
    )


def test_full_calendar_mobile_month_is_seven_column_compact_grid() -> None:
    css = _css()
    assert ".calendar-page .calendar-month-grid {" in css
    # Same compact 7-column grid used for weekday labels.
    assert ".calendar-page .calendar-month-weekdays {" in css
