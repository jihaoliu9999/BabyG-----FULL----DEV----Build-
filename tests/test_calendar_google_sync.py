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
