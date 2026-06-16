"""google_calendar.update_primary_event + delete_primary_event tests.

HTTP mocked at the httpx boundary. Covers partial PATCH semantics,
DELETE behaviour, error mapping, and event_id sanitization.

The create_primary_event happy path is already exercised indirectly
in test_bot.py (calendar.create_event staging/exec); this file is
focused on the new write paths.
"""

from __future__ import annotations

import httpx
import pytest

from app.integrations import google_calendar


def _ok_response(payload=None):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload or {}

    return _Resp()


def _err_response(status):
    class _Resp:
        def __init__(self):
            self.status_code = status

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("PATCH", "https://www.googleapis.com/test"),
                response=self,
            )

    return _Resp()


# ---------------------------------------------------------------------------
# update_primary_event
# ---------------------------------------------------------------------------


def test_update_primary_event_requires_event_id():
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.update_primary_event("tok", event_id="", title="x")


def test_update_primary_event_rejects_event_id_with_invalid_chars():
    """Path separators and whitespace are never valid in Google event
    IDs and would silently target an unintended URL if forwarded."""
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.update_primary_event(
            "tok", event_id="evt/abc", title="x"
        )
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.update_primary_event(
            "tok", event_id="evt abc", title="x"
        )


def test_update_primary_event_requires_at_least_one_field():
    """An update with no fields would be a no-op against Google and
    almost always indicates a bug upstream — refuse early."""
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.update_primary_event("tok", event_id="evt_1")


def test_update_primary_event_sends_only_provided_fields(monkeypatch):
    captured: dict = {}

    def _patch(url, json=None, headers=None, timeout=None):
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = dict(headers or {})
        return _ok_response({"id": "evt_1"})

    monkeypatch.setattr(httpx, "patch", _patch)

    out = google_calendar.update_primary_event(
        "TOKEN",
        event_id="evt_1",
        starts_at="2026-07-08T16:00:00-04:00",
    )

    assert out == "evt_1"
    assert captured["url"].endswith("/calendars/primary/events/evt_1")
    assert captured["headers"]["Authorization"] == "Bearer TOKEN"
    body = captured["json"]
    # Only start (and the auto-defaulted end) were sent — title /
    # description / location were not touched.
    assert "start" in body
    assert "end" in body
    assert "summary" not in body
    assert "description" not in body
    assert "location" not in body


def test_update_primary_event_keeps_title_and_clears_other_fields_independently(
    monkeypatch,
):
    captured: dict = {}

    def _patch(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _ok_response({"id": "evt_2"})

    monkeypatch.setattr(httpx, "patch", _patch)

    google_calendar.update_primary_event(
        "TOKEN",
        event_id="evt_2",
        title="Renamed call",
        location="Zoom",
    )

    body = captured["json"]
    assert body["summary"] == "Renamed call"
    assert body["location"] == "Zoom"
    # No start/end provided → not in the body.
    assert "start" not in body
    assert "end" not in body


def test_update_primary_event_normalizes_ends_at(monkeypatch):
    """Both start AND end fields are sent when ends_at is supplied.
    _clean_datetime normalizes the timezone (the same behavior the
    create path uses), so we check the body has both fields with a
    non-empty dateTime rather than pinning a specific format."""
    captured: dict = {}

    def _patch(url, json=None, headers=None, timeout=None):
        captured["json"] = json
        return _ok_response({"id": "evt_3"})

    monkeypatch.setattr(httpx, "patch", _patch)
    google_calendar.update_primary_event(
        "TOKEN",
        event_id="evt_3",
        starts_at="2026-07-08T10:00:00-04:00",
        ends_at="2026-07-08T11:30:00-04:00",
    )
    assert captured["json"]["start"]["dateTime"]
    assert captured["json"]["end"]["dateTime"]
    # End must be after start lexicographically when in the same TZ
    # normalization (UTC Z), so this catches obvious swaps.
    assert (
        captured["json"]["end"]["dateTime"]
        > captured["json"]["start"]["dateTime"]
    )


def test_update_primary_event_maps_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "patch", lambda *a, **kw: _err_response(404))
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.update_primary_event(
            "TOKEN", event_id="evt_404", title="missing"
        )


# ---------------------------------------------------------------------------
# delete_primary_event
# ---------------------------------------------------------------------------


def test_delete_primary_event_requires_event_id():
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.delete_primary_event("tok", event_id="")


def test_delete_primary_event_rejects_event_id_with_invalid_chars():
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.delete_primary_event("tok", event_id="evt#1")


def test_delete_primary_event_targets_event_url_with_bearer(monkeypatch):
    captured: dict = {}

    def _delete(url, headers=None, timeout=None):
        captured["url"] = url
        captured["headers"] = dict(headers or {})
        return _ok_response()

    monkeypatch.setattr(httpx, "delete", _delete)
    out = google_calendar.delete_primary_event("TOKEN", event_id="evt_99")
    assert out == "evt_99"
    assert captured["url"].endswith("/calendars/primary/events/evt_99")
    assert captured["headers"]["Authorization"] == "Bearer TOKEN"


def test_delete_primary_event_maps_http_error(monkeypatch):
    def _delete(url, headers=None, timeout=None):
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("DELETE", url),
            response=_err_response(500),
        )

    monkeypatch.setattr(httpx, "delete", _delete)
    with pytest.raises(google_calendar.GoogleCalendarError):
        google_calendar.delete_primary_event("TOKEN", event_id="evt_99")


# ---------------------------------------------------------------------------
# log hygiene — token / event content never in log records
# ---------------------------------------------------------------------------


def test_update_no_token_or_content_in_logs(monkeypatch, caplog):
    monkeypatch.setattr(httpx, "patch", lambda *a, **kw: _err_response(500))
    secret_title = "SECRET-UPDATE-TITLE"
    with caplog.at_level("DEBUG"), pytest.raises(
        google_calendar.GoogleCalendarError
    ):
        google_calendar.update_primary_event(
            "TOKEN-LEAK",
            event_id="evt_x",
            title=secret_title,
            notes=secret_title,
        )
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN-LEAK" not in text
    assert secret_title not in text


def test_delete_no_token_in_logs(monkeypatch, caplog):
    def _delete(url, headers=None, timeout=None):
        raise httpx.HTTPStatusError(
            "boom",
            request=httpx.Request("DELETE", url),
            response=_err_response(500),
        )

    monkeypatch.setattr(httpx, "delete", _delete)
    with caplog.at_level("DEBUG"), pytest.raises(
        google_calendar.GoogleCalendarError
    ):
        google_calendar.delete_primary_event("TOKEN-LEAK", event_id="evt_y")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN-LEAK" not in text
