from __future__ import annotations

from typing import Any

from app.integrations import google_calendar
from app.services import calendar_sync


class _Resp:
    def __init__(self, payload: dict[str, Any], status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def test_google_events_list_paginates_and_tags_calendar(monkeypatch):
    calls: list[dict[str, Any]] = []

    def _get(url, *, params, headers, timeout):
        calls.append({"url": url, "params": params})
        if len(calls) == 1:
            return _Resp({"items": [{"id": "a"}], "nextPageToken": "next"})
        return _Resp({"items": [{"id": "b"}]})

    monkeypatch.setattr(google_calendar.httpx, "get", _get)

    rows = google_calendar.list_events(
        "tok",
        calendar_id="cal-1",
        max_results=999,
    )

    assert [row["id"] for row in rows] == ["a", "b"]
    assert all(row["_babyg_calendar_id"] == "cal-1" for row in rows)
    assert calls[0]["params"]["singleEvents"] == "true"
    assert calls[0]["params"]["showDeleted"] == "true"
    assert calls[0]["params"]["maxResults"] == "250"
    assert calls[1]["params"]["pageToken"] == "next"


def test_event_payload_preserves_all_day_timezone_and_calendar():
    payload = google_calendar.event_to_booking_payload(
        {
            "id": "evt-1",
            "_babyg_calendar_id": "cal-1",
            "summary": "All day shoot",
            "status": "confirmed",
            "start": {"date": "2026-09-10", "timeZone": "America/New_York"},
            "end": {"date": "2026-09-11", "timeZone": "America/New_York"},
            "recurringEventId": "series-1",
            "originalStartTime": {"date": "2026-09-10"},
        }
    )

    assert payload is not None
    assert payload["google_calendar_id"] == "cal-1"
    assert payload["is_all_day"] is True
    assert payload["google_timezone"] == "America/New_York"
    assert payload["google_recurring_event_id"] == "series-1"


def test_sync_cancels_deleted_google_events(monkeypatch):
    calls: dict[str, Any] = {}

    monkeypatch.setattr(calendar_sync.oauth_connections, "access_token_for_google", lambda _u: "tok")
    monkeypatch.setattr(
        calendar_sync.google_calendar,
        "get_primary_calendar_timezone",
        lambda _t: None,
    )
    monkeypatch.setattr(calendar_sync.google_calendar, "list_calendar_ids", lambda _t: ["cal-1"])
    monkeypatch.setattr(
        calendar_sync.google_calendar,
        "list_events",
        lambda *_a, **_kw: [
            {
                "id": "evt-1",
                "_babyg_calendar_id": "cal-1",
                "status": "cancelled",
                "start": {"dateTime": "2026-09-10T10:00:00Z"},
            }
        ],
    )

    def _cancel(**kwargs):
        calls["cancel"] = kwargs
        return True

    monkeypatch.setattr(calendar_sync.bookings, "cancel_google_event", _cancel)

    result = calendar_sync.sync_google_calendar("user-1")

    assert result.imported == 1
    assert calls["cancel"]["user_id"] == "user-1"
    assert calls["cancel"]["google_calendar_id"] == "cal-1"
    assert calls["cancel"]["google_event_id"] == "evt-1"


# ---- maybe_auto_sync throttle ---------------------------------------


def test_maybe_auto_sync_first_call_fires(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module,
        "sync_google_calendar",
        lambda uid: calendar_sync_module.CalendarSyncResult(imported=2, connected=True),
    )
    calendar_sync_module._LAST_AUTO_SYNC_AT.clear()
    result = calendar_sync_module.maybe_auto_sync("user-a")
    assert result is not None
    assert result.imported == 2
    assert result.connected is True


def test_maybe_auto_sync_second_call_within_window_throttled(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module,
        "sync_google_calendar",
        lambda uid: calendar_sync_module.CalendarSyncResult(imported=1, connected=True),
    )
    calendar_sync_module._LAST_AUTO_SYNC_AT.clear()
    r1 = calendar_sync_module.maybe_auto_sync("user-a")
    r2 = calendar_sync_module.maybe_auto_sync("user-a")
    assert r1 is not None
    assert r2 is None


def test_maybe_auto_sync_per_user_isolation(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module,
        "sync_google_calendar",
        lambda uid: calendar_sync_module.CalendarSyncResult(imported=1, connected=True),
    )
    calendar_sync_module._LAST_AUTO_SYNC_AT.clear()
    a = calendar_sync_module.maybe_auto_sync("user-a")
    b = calendar_sync_module.maybe_auto_sync("user-b")
    assert a is not None
    assert b is not None


def test_maybe_auto_sync_swallows_sync_exceptions(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    def _boom(uid):
        raise RuntimeError("google down")

    monkeypatch.setattr(calendar_sync_module, "sync_google_calendar", _boom)
    calendar_sync_module._LAST_AUTO_SYNC_AT.clear()
    result = calendar_sync_module.maybe_auto_sync("user-a")
    assert result is None


def test_maybe_auto_sync_empty_user_id_returns_none(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    called = {"n": 0}

    def _sync(uid):
        called["n"] += 1
        return calendar_sync_module.CalendarSyncResult(imported=0, connected=True)

    monkeypatch.setattr(calendar_sync_module, "sync_google_calendar", _sync)
    calendar_sync_module._LAST_AUTO_SYNC_AT.clear()
    assert calendar_sync_module.maybe_auto_sync("") is None
    assert called["n"] == 0


# ---- diagnostic logging --------------------------------------------
# The following tests lock the safe-ID-hint log lines that let us
# diagnose live production sync failures without needing to reproduce
# them locally. If sync starts failing again, we need production logs
# to say exactly where.


def test_sync_emits_start_calendars_and_complete_logs(monkeypatch, caplog):
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: "TOKEN",
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "get_primary_calendar_timezone",
        lambda token: None,
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_calendar_ids",
        lambda token: ["primary", "work@group.calendar.google.com"],
    )

    def _list_events(token, *, calendar_id, time_min, time_max, max_results):
        if calendar_id == "primary":
            return [
                {"id": "e-1", "start": {"dateTime": "2026-09-10T10:00:00Z"}, "_babyg_calendar_id": calendar_id},
            ]
        return []

    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_events",
        _list_events,
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "event_to_booking_payload",
        lambda ev, **_kw: {
            "google_calendar_id": ev["_babyg_calendar_id"],
            "google_event_id": ev["id"],
            "starts_at": "2026-09-10T10:00:00Z",
            "status": "confirmed",
        },
    )
    monkeypatch.setattr(
        calendar_sync_module.bookings,
        "upsert_google_event",
        lambda *, user_id, payload: True,
    )

    import logging
    with caplog.at_level(logging.INFO):
        result = calendar_sync_module.sync_google_calendar("creator-1")

    assert result.imported == 1
    assert result.error is None
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    # Sync start log
    assert "calendar_sync.start user=creator-1" in log_text
    # Per-calendar counts (safe hint, not full id)
    assert "calendar_sync.calendars user=creator-1 count=2" in log_text
    assert "calendar_sync.events_per_calendar" in log_text
    # Sync completion with import/skip/cancel breakdown
    assert "calendar_sync.complete" in log_text
    assert "imported=1" in log_text


def test_sync_error_log_names_reason(monkeypatch, caplog):
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: "TOKEN",
    )

    def _boom(token):
        raise calendar_sync_module.google_calendar.GoogleCalendarError("403")

    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "get_primary_calendar_timezone",
        lambda token: None,
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_calendar_ids",
        _boom,
    )

    import logging
    with caplog.at_level(logging.INFO):
        result = calendar_sync_module.sync_google_calendar("creator-2")

    assert result.error == "google_error"
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "calendar_sync.error user=creator-2 reason=google_error" in log_text


def test_sync_not_connected_logs_reason(monkeypatch, caplog):
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: None,
    )
    import logging
    with caplog.at_level(logging.INFO):
        result = calendar_sync_module.sync_google_calendar("creator-3")

    assert result.error == "not_connected"
    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "calendar_sync.error user=creator-3 reason=not_connected" in log_text


# ---- timezone correctness ------------------------------------------
# Anchors for the "wrong day highlighted" and "all-day event drifted"
# bugs. Server UTC must never determine the visible day.


def test_get_primary_calendar_timezone_returns_zone(monkeypatch):
    calls: list[str] = []

    def _get(url, *, headers, timeout):
        calls.append(url)
        return _Resp({"id": "primary", "timeZone": "America/New_York"})

    monkeypatch.setattr(google_calendar.httpx, "get", _get)

    tz = google_calendar.get_primary_calendar_timezone("tok")

    assert tz == "America/New_York"
    assert calls[0].endswith("/calendars/primary")


def test_get_primary_calendar_timezone_returns_none_on_error(monkeypatch):
    import httpx as _httpx

    def _get(url, *, headers, timeout):
        request = _httpx.Request("GET", url)
        response = _httpx.Response(401, request=request)
        raise _httpx.HTTPStatusError("401", request=request, response=response)

    monkeypatch.setattr(google_calendar.httpx, "get", _get)

    assert google_calendar.get_primary_calendar_timezone("tok") is None


def test_all_day_event_anchors_to_calendar_timezone_not_utc():
    """The wrong-day bug: an all-day event on Sep 10 stored at UTC
    midnight resolves to Sep 9 in New York. Anchor midnight to the
    calendar's local zone so the local calendar date is preserved."""
    payload = google_calendar.event_to_booking_payload(
        {
            "id": "evt-1",
            "_babyg_calendar_id": "cal-1",
            "summary": "All-day shoot",
            "status": "confirmed",
            "start": {"date": "2026-09-10"},
            "end": {"date": "2026-09-11"},
        },
        default_timezone="America/New_York",
    )
    assert payload is not None
    assert payload["is_all_day"] is True
    assert payload["google_timezone"] == "America/New_York"
    # Sep 10 midnight NY (EDT = UTC-4) — NOT Sep 10 UTC midnight
    # which would resolve to Sep 9 20:00 EDT.
    assert payload["starts_at"].startswith("2026-09-10T00:00:00")
    assert "-04:00" in payload["starts_at"] or "-05:00" in payload["starts_at"]


def test_all_day_event_prefers_event_timezone_over_default():
    payload = google_calendar.event_to_booking_payload(
        {
            "id": "evt-2",
            "_babyg_calendar_id": "cal-1",
            "start": {"date": "2026-09-10", "timeZone": "America/Los_Angeles"},
            "end": {"date": "2026-09-11", "timeZone": "America/Los_Angeles"},
        },
        default_timezone="America/New_York",
    )
    assert payload is not None
    assert payload["google_timezone"] == "America/Los_Angeles"
    assert "-07:00" in payload["starts_at"] or "-08:00" in payload["starts_at"]


def test_all_day_event_falls_back_to_utc_when_no_timezone():
    """No timezone information anywhere — UTC is the safe fallback.
    Callers that care about the visible day must supply a timezone."""
    payload = google_calendar.event_to_booking_payload(
        {
            "id": "evt-3",
            "_babyg_calendar_id": "cal-1",
            "start": {"date": "2026-09-10"},
            "end": {"date": "2026-09-11"},
        }
    )
    assert payload is not None
    assert payload["is_all_day"] is True
    assert payload["google_timezone"] is None
    assert payload["starts_at"].startswith("2026-09-10T00:00:00")
    assert "+00:00" in payload["starts_at"]


def test_sync_passes_primary_timezone_to_payload_builder(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    captured: dict[str, Any] = {}

    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda _u: "tok",
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "get_primary_calendar_timezone",
        lambda _t: "America/New_York",
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_calendar_ids",
        lambda _t: ["primary"],
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_events",
        lambda *_a, **_kw: [
            {"id": "e1", "_babyg_calendar_id": "primary", "start": {"date": "2026-09-10"}}
        ],
    )

    def _payload(ev, *, default_timezone=None):
        captured["default_timezone"] = default_timezone
        return {
            "google_event_id": ev["id"],
            "google_calendar_id": ev["_babyg_calendar_id"],
            "starts_at": "2026-09-10T00:00:00-04:00",
            "status": "confirmed",
        }

    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "event_to_booking_payload",
        _payload,
    )
    monkeypatch.setattr(
        calendar_sync_module.bookings,
        "upsert_google_event",
        lambda *, user_id, payload: True,
    )

    calendar_sync_module.sync_google_calendar("user-tz-1")

    assert captured["default_timezone"] == "America/New_York"


def test_effective_timezone_returns_google_primary(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    calendar_sync_module._TIMEZONE_CACHE.clear()
    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: "tok",
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "get_primary_calendar_timezone",
        lambda _t: "America/New_York",
    )

    assert calendar_sync_module.effective_timezone("u-1") == "America/New_York"


def test_effective_timezone_none_when_no_token(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    calendar_sync_module._TIMEZONE_CACHE.clear()
    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: None,
    )
    assert calendar_sync_module.effective_timezone("u-2") is None


def test_effective_timezone_cached_across_calls(monkeypatch):
    from app.services import calendar_sync as calendar_sync_module

    calendar_sync_module._TIMEZONE_CACHE.clear()
    calls = {"n": 0}

    def _lookup(_t):
        calls["n"] += 1
        return "Europe/London"

    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: "tok",
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "get_primary_calendar_timezone",
        _lookup,
    )
    a = calendar_sync_module.effective_timezone("u-3")
    b = calendar_sync_module.effective_timezone("u-3")
    assert a == "Europe/London"
    assert b == "Europe/London"
    assert calls["n"] == 1


def test_today_in_zone_uses_named_zone():
    from app.services import calendar_sync as calendar_sync_module

    # Same wall-clock moment resolves to different calendar days
    # depending on the zone. Any tz-aware "today" fits into a
    # +/- 1-day window around UTC's current date.
    today_ny = calendar_sync_module.today_in_zone("America/New_York")
    today_utc = calendar_sync_module.today_in_zone(None)
    assert abs((today_utc - today_ny).days) <= 1


def test_today_in_zone_unknown_zone_falls_back_to_utc():
    from app.services import calendar_sync as calendar_sync_module

    fallback = calendar_sync_module.today_in_zone("Not/A/Zone")
    utc_today = calendar_sync_module.today_in_zone(None)
    assert fallback == utc_today


def test_sync_id_hint_never_leaks_full_calendar_id(monkeypatch, caplog):
    """The log helper masks all but the first 6 + last 6 characters of
    each opaque id. Verify a Google-style calendar id never appears
    fully in log output."""
    from app.services import calendar_sync as calendar_sync_module

    monkeypatch.setattr(
        calendar_sync_module.oauth_connections,
        "access_token_for_google",
        lambda uid: "TOKEN",
    )
    long_cal_id = "verylongcalendarid_1234567890@group.calendar.google.com"
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "get_primary_calendar_timezone",
        lambda token: None,
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_calendar_ids",
        lambda token: [long_cal_id],
    )
    monkeypatch.setattr(
        calendar_sync_module.google_calendar,
        "list_events",
        lambda token, **kw: [],
    )

    import logging
    with caplog.at_level(logging.INFO):
        calendar_sync_module.sync_google_calendar("creator-4")

    log_text = "\n".join(rec.getMessage() for rec in caplog.records)
    # Full id must not appear
    assert long_cal_id not in log_text
    # Hint format present
    assert "verylo" in log_text
    assert "le.com" in log_text
