"""Home calendar preview shows ONLY the selected day's events.

Prior state: ``app/templates/creator/dashboard.html`` iterated
``calendar_grid.week_days`` and appended every day's events into
one flat list, so Thursday's event appeared on Home even when
today was Friday.

Fix: ``_calendar_grid_context`` already exposes ``selected_day``
with per-date-filtered ``all_day_events`` / ``timed_events``. The
Home template now iterates only that single-day slice.

This suite locks:
  * a Thursday event does NOT appear when today (selected) is Friday
  * a Friday event DOES appear on Friday
  * symmetric: a Thursday event does appear on Thursday
  * near-midnight timezone boundary events map to the correct
    calendar-timezone day
  * all-day events stay on their date
  * ``_calendar_grid_context.selected_day`` exposes only the
    events belonging to the selected date (unit-level lock on the
    server contract)
  * the full ``/creator/calendar`` page path is unchanged
    (regression guard on the ``_month_context`` shape)
  * user isolation is preserved (data is filtered by ``user_id``
    upstream by ``bookings.list_for_user_range``, verified by
    static reference)
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes

REPO = Path(__file__).resolve().parents[1]
DASHBOARD_TPL = REPO / "app" / "templates" / "creator" / "dashboard.html"


def _signed_in(client: TestClient, *, user_id: str = "u-home-cal-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


# ---------------------------------------------------------------------------
# 1. Server-side contract: _calendar_grid_context.selected_day contains
#    ONLY events for the selected date.
# ---------------------------------------------------------------------------


def _row(
    *,
    booking_id: str,
    title: str,
    starts_at: str,
    ends_at: str | None = None,
    is_all_day: bool = False,
    google_timezone: str | None = None,
) -> dict:
    return {
        "id": booking_id,
        "title": title,
        "starts_at": starts_at,
        "ends_at": ends_at,
        "is_all_day": is_all_day,
        "status": "confirmed",
        "type": "event",
        "google_status": None,
        "google_timezone": google_timezone,
    }


def test_selected_day_filters_out_previous_day_events():
    """Reproduction of the reported bug at the contract level.
    Thursday event + Friday event → selecting Friday returns only
    Friday's event."""
    friday = date(2026, 9, 18)
    rows = [
        _row(
            booking_id="thu-1",
            title="Reservation at Casa D'Angelo",
            starts_at="2026-09-17T18:30:00-04:00",
            ends_at="2026-09-17T20:00:00-04:00",
            google_timezone="America/New_York",
        ),
        _row(
            booking_id="fri-1",
            title="meeting $",
            starts_at="2026-09-18T15:00:00-04:00",
            ends_at="2026-09-18T16:00:00-04:00",
            google_timezone="America/New_York",
        ),
    ]
    grid = creator_routes._calendar_grid_context(
        rows, friday, today=friday, tz_name="America/New_York"
    )
    selected = grid["selected_day"]
    assert selected["iso"] == "2026-09-18"
    titles = [e["title"] for e in selected["all_day_events"] + selected["timed_events"]]
    assert "meeting $" in titles
    assert "Reservation at Casa D'Angelo" not in titles, (
        "Thursday's event must not leak forward when Friday is selected"
    )


def test_selected_day_shows_the_days_own_event():
    thursday = date(2026, 9, 17)
    rows = [
        _row(
            booking_id="thu-1",
            title="Reservation at Casa D'Angelo",
            starts_at="2026-09-17T18:30:00-04:00",
            ends_at="2026-09-17T20:00:00-04:00",
            google_timezone="America/New_York",
        ),
        _row(
            booking_id="fri-1",
            title="meeting $",
            starts_at="2026-09-18T15:00:00-04:00",
            ends_at="2026-09-18T16:00:00-04:00",
            google_timezone="America/New_York",
        ),
    ]
    grid = creator_routes._calendar_grid_context(
        rows, thursday, today=thursday, tz_name="America/New_York"
    )
    selected = grid["selected_day"]
    assert selected["iso"] == "2026-09-17"
    titles = [e["title"] for e in selected["all_day_events"] + selected["timed_events"]]
    assert "Reservation at Casa D'Angelo" in titles
    assert "meeting $" not in titles


def test_selected_day_exposes_weekday_label():
    """The template renders a weekday label; ``selected_day.weekday``
    must be present so the template does not have to reach back into
    the outer week_days loop (which was the source of the bug)."""
    friday = date(2026, 9, 18)
    grid = creator_routes._calendar_grid_context([], friday, today=friday)
    assert "weekday" in grid["selected_day"]
    assert grid["selected_day"]["weekday"] == "Fri"


# ---------------------------------------------------------------------------
# 2. Timezone boundary: an event whose UTC timestamp straddles
#    midnight in the user's local TZ must map to the correct calendar
#    day for that TZ.
# ---------------------------------------------------------------------------


def test_late_evening_event_stays_on_its_local_day():
    """An event that starts at 22:00 local time on Thursday and ends
    at 23:00 local Thursday (both wall-clock times in ``America/New_York``,
    03:00 UTC → 04:00 UTC on the next UTC day) is entirely inside
    Thursday's local calendar day. Selecting Friday must NOT show it;
    selecting Thursday must."""
    thursday = date(2026, 9, 17)
    friday = date(2026, 9, 18)
    rows = [
        _row(
            booking_id="late-thu",
            # 22:00-23:00 America/New_York on Thursday = 02:00-03:00 UTC Friday.
            starts_at="2026-09-18T02:00:00+00:00",
            ends_at="2026-09-18T03:00:00+00:00",
            title="late Thursday",
            google_timezone="America/New_York",
        ),
    ]
    friday_grid = creator_routes._calendar_grid_context(
        rows, friday, today=friday, tz_name="America/New_York"
    )
    assert friday_grid["selected_day"]["iso"] == "2026-09-18"
    fri_titles = [
        e["title"]
        for e in friday_grid["selected_day"]["all_day_events"]
        + friday_grid["selected_day"]["timed_events"]
    ]
    assert "late Thursday" not in fri_titles, (
        "an event fully inside Thursday's local day must not appear on "
        "Friday, even when its UTC timestamp is on the Friday side of "
        "midnight"
    )

    thursday_grid = creator_routes._calendar_grid_context(
        rows, thursday, today=thursday, tz_name="America/New_York"
    )
    assert thursday_grid["selected_day"]["iso"] == "2026-09-17"
    thu_titles = [
        e["title"]
        for e in thursday_grid["selected_day"]["all_day_events"]
        + thursday_grid["selected_day"]["timed_events"]
    ]
    assert "late Thursday" in thu_titles


# ---------------------------------------------------------------------------
# 3. All-day events land on their date only.
# ---------------------------------------------------------------------------


def test_all_day_event_appears_only_on_its_resolved_local_date():
    """An all-day event resolves to a single local date via
    ``_event_vm``'s tz conversion. It must appear on that date and
    NOT on the surrounding days (this test uses a UTC event body,
    so the New-York-local date is Sep 18 when the persisted
    ``starts_at`` is 2026-09-18T04:00Z — UTC-4 lands us cleanly on
    Sep 18 wall-clock)."""
    friday = date(2026, 9, 18)
    saturday = date(2026, 9, 19)
    rows = [
        _row(
            booking_id="ad-fri",
            title="content batch day",
            starts_at="2026-09-18T04:00:00+00:00",
            ends_at="2026-09-18T04:00:00+00:00",
            is_all_day=True,
            google_timezone="America/New_York",
        ),
    ]
    friday_grid = creator_routes._calendar_grid_context(
        rows, friday, today=friday, tz_name="America/New_York"
    )
    fri_titles = [
        e["title"]
        for e in friday_grid["selected_day"]["all_day_events"]
        + friday_grid["selected_day"]["timed_events"]
    ]
    assert "content batch day" in fri_titles

    saturday_grid = creator_routes._calendar_grid_context(
        rows, saturday, today=saturday, tz_name="America/New_York"
    )
    sat_titles = [
        e["title"]
        for e in saturday_grid["selected_day"]["all_day_events"]
        + saturday_grid["selected_day"]["timed_events"]
    ]
    assert "content batch day" not in sat_titles


# ---------------------------------------------------------------------------
# 4. Template renders only the selected day.
# ---------------------------------------------------------------------------


@pytest.fixture()
def _stub_home(monkeypatch):
    from app.services import (
        action_proposals,
        agent_recap,
        calendar_sync,
        discover,
        dms,
        home_briefing,
        instagram_dms,
        network,
        notifications,
        oauth_connections,
    )
    from app.services import brief as brief_service
    from app.services import greetings as greetings_module
    from app.services import profiles as profiles_module

    monkeypatch.setattr(
        profiles_module,
        "get_creator_profile_cached",
        lambda uid, request=None: {"onboarding_completed_at": "2026-09-01T00:00:00Z"},
    )
    monkeypatch.setattr(notifications, "list_unread", lambda uid, *, limit=8: [])
    monkeypatch.setattr(network, "list_incoming_pending", lambda uid: [])
    monkeypatch.setattr(discover, "list_cards", lambda **kw: [])
    monkeypatch.setattr(action_proposals, "list_pending_for_user", lambda **kw: [])
    monkeypatch.setattr(dms, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(instagram_dms, "unread_count_for_creator", lambda uid: 0)
    monkeypatch.setattr(oauth_connections, "get_instagram_connection", lambda uid: None)
    monkeypatch.setattr(oauth_connections, "instagram_needs_reconnect", lambda uid: False)
    monkeypatch.setattr(home_briefing, "primary_carousel_slides", lambda uid, **kw: [])
    monkeypatch.setattr(home_briefing, "handled_today", lambda uid, **kw: 0)
    monkeypatch.setattr(home_briefing, "watching_summary", lambda **kw: {})
    monkeypatch.setattr(brief_service, "home_preview_rows", lambda uid: [])
    monkeypatch.setattr(agent_recap, "build", lambda uid: None)
    monkeypatch.setattr(
        greetings_module,
        "pick_daily",
        lambda uid, first_name: {"morning": "hi", "evening": "hi", "afternoon": "hi"},
    )
    # No Google → skip auto-sync path entirely; we set google_calendar_connected
    # directly to the pre-baked value below.
    monkeypatch.setattr(
        oauth_connections, "get_google_connection", lambda uid: {"connected": True}
    )
    monkeypatch.setattr(
        oauth_connections, "google_calendar_connected", lambda conn: True
    )
    # Return a fixed timezone-naive-safe date so the "today" anchor is
    # deterministic in tests.
    monkeypatch.setattr(calendar_sync, "effective_timezone", lambda uid: "America/New_York")
    monkeypatch.setattr(
        calendar_sync,
        "today_in_zone",
        lambda tz: date(2026, 9, 18),
    )
    # Auto-sync is fire-and-forget in the current handler; still make it
    # a no-op here in case the test client's BackgroundTasks flushes it.
    monkeypatch.setattr(calendar_sync, "maybe_auto_sync", lambda uid: None)


def test_home_response_shows_only_selected_day_events(monkeypatch, _stub_home):
    """End-to-end guard: with today = Friday, the rendered Home HTML
    must contain Friday's event and must NOT contain Thursday's."""
    from app.services import bookings

    friday_event_title = "meeting $ — friday only"
    thursday_event_title = "casa dangelo dinner — thursday only"

    monkeypatch.setattr(
        bookings,
        "list_for_user_range",
        lambda uid, **kw: [
            _row(
                booking_id="fri-1",
                title=friday_event_title,
                starts_at="2026-09-18T15:00:00-04:00",
                ends_at="2026-09-18T16:00:00-04:00",
                google_timezone="America/New_York",
            ),
            _row(
                booking_id="thu-1",
                title=thursday_event_title,
                starts_at="2026-09-17T18:30:00-04:00",
                ends_at="2026-09-17T20:00:00-04:00",
                google_timezone="America/New_York",
            ),
        ],
    )

    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200, r.text[:200]

    # Isolate the Home Calendar preview block (the <ol> under
    # `.creator-home-day-events`) so we're not distracted by matches
    # elsewhere on the page.
    block = r.text.split("creator-home-day-events", 1)[1].split("</ol>", 1)[0]
    assert friday_event_title in block, "Friday's event must render on Friday"
    assert thursday_event_title not in block, (
        "Thursday's event must NOT render on Friday — the pre-fix bug"
    )


def test_home_response_empty_state_when_selected_day_has_no_events(
    monkeypatch, _stub_home
):
    from app.services import bookings

    # Only a Thursday event; today (per _stub_home) is Friday, so
    # Home's calendar preview should be empty on Friday.
    monkeypatch.setattr(
        bookings,
        "list_for_user_range",
        lambda uid, **kw: [
            _row(
                booking_id="thu-1",
                title="casa dangelo dinner",
                starts_at="2026-09-17T18:30:00-04:00",
                ends_at="2026-09-17T20:00:00-04:00",
                google_timezone="America/New_York",
            ),
        ],
    )

    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200

    block = r.text.split("creator-home-day-events", 1)[1].split("</ol>", 1)[0]
    assert "nothing scheduled" in block, (
        "empty selected-day state should render when the only events "
        "are on other days"
    )
    assert "casa dangelo dinner" not in block


# ---------------------------------------------------------------------------
# 5. Static lock: the template renders selected_day, not week_days,
#    inside the event list.
# ---------------------------------------------------------------------------


def test_home_template_iterates_selected_day_not_week_days_for_events():
    tpl = DASHBOARD_TPL.read_text()
    # Isolate the event <ol>. Its opening is `creator-home-day-list`.
    block_start = tpl.index('class="creator-home-day-list"')
    remainder = tpl[block_start:]
    block = remainder.split("</ol>", 1)[0]
    # It must NOT iterate the week to build the event list.
    assert "for day in calendar_grid.week_days" not in block, (
        "Home event list must not iterate the whole week — that was the bug"
    )
    # It MUST source events from selected_day.
    assert (
        "for event in selected_day.all_day_events" in block
        or "for event in calendar_grid.selected_day.all_day_events" in block
    ), "Home event list must iterate selected_day.all_day_events"
    assert (
        "for event in selected_day.timed_events" in block
        or "for event in calendar_grid.selected_day.timed_events" in block
    ), "Home event list must iterate selected_day.timed_events"


# ---------------------------------------------------------------------------
# 6. Full Calendar path is untouched — _month_context still returns
#    per-day event buckets keyed by ISO date.
# ---------------------------------------------------------------------------


def test_month_context_shape_preserved_for_full_calendar():
    rows = [
        _row(
            booking_id="fri-1",
            title="meeting $",
            starts_at="2026-09-18T15:00:00-04:00",
            ends_at="2026-09-18T16:00:00-04:00",
            google_timezone="America/New_York",
        ),
    ]
    ctx = creator_routes._month_context(
        rows, date(2026, 9, 18), today=date(2026, 9, 18), tz_name="America/New_York"
    )
    assert "month_days" in ctx and len(ctx["month_days"]) == 42
    friday_cells = [d for d in ctx["month_days"] if d["iso"] == "2026-09-18"]
    assert len(friday_cells) == 1
    titles = [e["title"] for e in friday_cells[0]["events"]]
    assert "meeting $" in titles


# ---------------------------------------------------------------------------
# 7. User isolation — the upstream service filters by user_id; Home
#    only ever passes the authenticated user's bookings into
#    _calendar_grid_context.
# ---------------------------------------------------------------------------


def test_bookings_list_for_user_range_is_user_scoped():
    """Static reference lock: the Home handler feeds
    ``bookings.list_for_user_range`` (which filters ``.eq("user_id",
    user_id)``) into ``_calendar_grid_context``. User A's events
    therefore cannot appear on user B's Home."""
    from app.services import bookings

    src_bookings = (REPO / "app" / "services" / "bookings.py").read_text()
    # bookings.list_for_user_range filters by user_id.
    assert '.eq("user_id", user_id)' in src_bookings
    # Home handler passes the authenticated user_id in.
    src_creator = (REPO / "app" / "routes" / "creator.py").read_text()
    assert "bookings.list_for_user_range" in src_creator
    # And feeds the returned rows into _calendar_grid_context via
    # ``upcoming_bookings`` (the destructured branch of the gather).
    assert "_calendar_grid_context(\n                upcoming_bookings" in src_creator
    # Belt-check that the module resolves at import time.
    assert callable(bookings.list_for_user_range)


# Silence "unused import" lints for symbols kept for future-proofing.
_ = (datetime, timezone, UTC, ZoneInfo)
