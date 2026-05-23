"""Google Calendar OAuth + events client.

Only server code imports this module. It never exposes OAuth tokens to
templates or browser JavaScript.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
DEFAULT_CALLBACK_PATH = "/creator/google/calendar/callback"


class GoogleCalendarError(RuntimeError):
    """Raised for non-secret Google OAuth/API failures."""


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.google_client_id and settings.google_client_secret)


def redirect_uri() -> str:
    settings = get_settings()
    if settings.google_redirect_uri:
        return settings.google_redirect_uri
    return f"{settings.app_url.rstrip('/')}{DEFAULT_CALLBACK_PATH}"


def scopes() -> list[str]:
    raw = get_settings().google_oauth_scopes or ""
    parsed = [scope.strip() for scope in raw.replace(",", " ").split() if scope.strip()]
    if parsed:
        return parsed
    return ["https://www.googleapis.com/auth/calendar.events"]


def auth_url(state: str) -> str:
    if not is_configured():
        raise GoogleCalendarError("Google OAuth is not configured")
    params = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(scopes()),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    if not is_configured():
        raise GoogleCalendarError("Google OAuth is not configured")
    payload = {
        "code": code,
        "client_id": get_settings().google_client_id,
        "client_secret": get_settings().google_client_secret,
        "redirect_uri": redirect_uri(),
        "grant_type": "authorization_code",
    }
    return _post_token(payload)


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    if not is_configured():
        raise GoogleCalendarError("Google OAuth is not configured")
    payload = {
        "client_id": get_settings().google_client_id,
        "client_secret": get_settings().google_client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }
    return _post_token(payload)


def list_primary_events(
    access_token: str,
    *,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC)
    time_min = time_min or (now - timedelta(days=30))
    time_max = time_max or (now + timedelta(days=180))
    params = {
        "singleEvents": "true",
        "orderBy": "startTime",
        "timeMin": _google_dt(time_min),
        "timeMax": _google_dt(time_max),
        "maxResults": str(max(1, min(max_results, 250))),
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = httpx.get(EVENTS_URL, params=params, headers=headers, timeout=20.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google Calendar events.list failed with status %s", status)
        raise GoogleCalendarError("Google Calendar events request failed") from exc
    data = response.json()
    items = data.get("items", [])
    return items if isinstance(items, list) else []


def event_to_booking_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    event_id = str(event.get("id") or "").strip()
    if not event_id or event.get("status") == "cancelled":
        return None

    start = _event_time(event.get("start") or {})
    if not start:
        return None
    end = _event_time(event.get("end") or {})
    summary = str(event.get("summary") or "untitled event").strip()[:140]
    location = str(event.get("location") or "").strip()[:160]
    description = str(event.get("description") or "").strip()[:2000]

    return {
        "title": summary or "untitled event",
        "type": "event",
        "starts_at": start,
        "ends_at": end,
        "notes": description or None,
        "status": "confirmed",
        "venue_name": location or None,
        "google_calendar_id": "primary",
        "google_event_id": event_id,
    }


def _post_token(payload: dict[str, str]) -> dict[str, Any]:
    try:
        response = httpx.post(TOKEN_URL, data=payload, timeout=20.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google OAuth token exchange failed with status %s", status)
        raise GoogleCalendarError("Google OAuth token request failed") from exc
    data = response.json()
    if not isinstance(data, dict) or not data.get("access_token"):
        raise GoogleCalendarError("Google OAuth token response was missing access_token")
    return data


def _event_time(value: dict[str, Any]) -> str | None:
    if not isinstance(value, dict):
        return None
    date_time = value.get("dateTime")
    if date_time:
        return str(date_time)
    date_value = value.get("date")
    if not date_value:
        return None
    try:
        all_day = date.fromisoformat(str(date_value))
    except ValueError:
        return None
    return datetime(all_day.year, all_day.month, all_day.day, tzinfo=UTC).isoformat()


def _google_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
