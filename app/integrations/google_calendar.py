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
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SCOPE_PREFIX = "https://www.googleapis.com/auth/gmail."
CALENDAR_REQUIRED_SCOPES = (CALENDAR_SCOPE,)
GMAIL_REQUIRED_SCOPES = (
    GMAIL_READONLY_SCOPE,
    GMAIL_COMPOSE_SCOPE,
    GMAIL_SEND_SCOPE,
)
ALLOWED_OAUTH_SCOPES = frozenset((*CALENDAR_REQUIRED_SCOPES, *GMAIL_REQUIRED_SCOPES))


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
        return allowed_scopes(parsed) or [CALENDAR_SCOPE]
    return list(CALENDAR_REQUIRED_SCOPES)


def scopes_for_services(services: list[str]) -> list[str]:
    selected: list[str] = []
    if "calendar" in services:
        selected.extend(CALENDAR_REQUIRED_SCOPES)
    if "gmail" in services:
        # Gmail is split deliberately: readonly powers inbox/thread context,
        # compose stages approved drafts, and send gates approved sends.
        selected.extend(GMAIL_REQUIRED_SCOPES)
    return _dedupe(selected)


def auth_url(state: str, *, scopes_override: list[str] | None = None) -> str:
    if not is_configured():
        raise GoogleCalendarError("Google OAuth is not configured")
    selected_scopes = allowed_scopes(scopes_override or scopes())
    if not selected_scopes:
        raise GoogleCalendarError("Google OAuth scopes are not configured")
    params = {
        "client_id": get_settings().google_client_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": " ".join(selected_scopes),
        "state": state,
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def is_calendar_scope(scope: str) -> bool:
    return scope == CALENDAR_SCOPE


def is_gmail_scope(scope: str) -> bool:
    return scope.startswith(GMAIL_SCOPE_PREFIX)


def is_gmail_compose_scope(scope: str) -> bool:
    return scope == GMAIL_COMPOSE_SCOPE


def is_gmail_send_scope(scope: str) -> bool:
    return scope == GMAIL_SEND_SCOPE


def allowed_scopes(scopes_to_check: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    return _dedupe([scope for scope in scopes_to_check if scope in ALLOWED_OAUTH_SCOPES])


def has_calendar_scope(scopes_to_check: list[str] | set[str] | tuple[str, ...]) -> bool:
    return any(is_calendar_scope(scope) for scope in scopes_to_check)


def has_gmail_compose_scope(
    scopes_to_check: list[str] | set[str] | tuple[str, ...],
) -> bool:
    """True only when the compose scope is present. Read-only connections
    return False — they need to reconnect before drafts can be staged."""
    return any(is_gmail_compose_scope(scope) for scope in scopes_to_check)


def has_gmail_send_scope(
    scopes_to_check: list[str] | set[str] | tuple[str, ...],
) -> bool:
    """True only when gmail.send is present. Compose-only connections
    can draft but must reconnect before approved sends are available."""
    return any(is_gmail_send_scope(scope) for scope in scopes_to_check)


def has_gmail_read_scope(scopes_to_check: list[str] | set[str] | tuple[str, ...]) -> bool:
    return any(scope == GMAIL_READONLY_SCOPE for scope in scopes_to_check)


def has_gmail_scope(scopes_to_check: list[str] | set[str] | tuple[str, ...]) -> bool:
    return any(is_gmail_scope(scope) for scope in scopes_to_check)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


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


def create_primary_event(
    access_token: str,
    *,
    title: str,
    starts_at: str,
    ends_at: str | None = None,
    notes: str | None = None,
    location: str | None = None,
) -> str:
    """Create one Google Calendar event. Returns the Google event id.

    Must only be called by an approved action executor after explicit
    creator confirmation. This does not delete, update, invite guests,
    book restaurants, collect payment, or create paid reservations.
    """
    summary = " ".join(str(title or "").split())[:140]
    start = _clean_datetime(starts_at)
    end = _clean_datetime(ends_at) if ends_at else _default_end(start)
    if not summary or not start:
        raise GoogleCalendarError("Google Calendar event missing title or start")
    payload: dict[str, Any] = {
        "summary": summary,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    description = str(notes or "").strip()[:2000]
    venue = str(location or "").strip()[:160]
    if description:
        payload["description"] = description
    if venue:
        payload["location"] = venue

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.post(EVENTS_URL, json=payload, headers=headers, timeout=20.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google Calendar events.insert failed with status %s", status)
        raise GoogleCalendarError("Google Calendar event create failed") from exc
    data = response.json()
    event_id = str(data.get("id") or "")
    if not event_id:
        raise GoogleCalendarError("Google Calendar event create returned no id")
    return event_id


def update_primary_event(
    access_token: str,
    *,
    event_id: str,
    title: str | None = None,
    starts_at: str | None = None,
    ends_at: str | None = None,
    notes: str | None = None,
    location: str | None = None,
) -> str:
    """Partial-update one Google Calendar event the creator already owns.

    Only the fields explicitly provided are sent — Google's PATCH
    semantics leave omitted fields untouched. Returns the event id on
    success (echoes input; lets callers chain without re-parsing).

    Must only be called by an approved action executor after explicit
    creator confirmation. This does not invite guests, change
    organizers, attach payment, or escalate access.
    """
    clean_event_id = _clean_event_id(event_id)
    payload: dict[str, Any] = {}
    if title is not None:
        summary = " ".join(str(title).split())[:140]
        if not summary:
            raise GoogleCalendarError("Google Calendar update title empty")
        payload["summary"] = summary
    if starts_at is not None:
        start = _clean_datetime(starts_at)
        if not start:
            raise GoogleCalendarError("Google Calendar update starts_at invalid")
        payload["start"] = {"dateTime": start}
        # If a start was given without an explicit end, default end so
        # Google doesn't reject a half-updated time range.
        if ends_at is None:
            payload["end"] = {"dateTime": _default_end(start)}
    if ends_at is not None:
        end = _clean_datetime(ends_at)
        if not end:
            raise GoogleCalendarError("Google Calendar update ends_at invalid")
        payload["end"] = {"dateTime": end}
    if notes is not None:
        payload["description"] = str(notes or "").strip()[:2000] or ""
    if location is not None:
        payload["location"] = str(location or "").strip()[:160] or ""
    if not payload:
        raise GoogleCalendarError("Google Calendar update missing all fields")

    url = f"{EVENTS_URL}/{clean_event_id}"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    try:
        response = httpx.patch(url, json=payload, headers=headers, timeout=20.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google Calendar events.patch failed with status %s", status)
        raise GoogleCalendarError("Google Calendar event update failed") from exc
    return clean_event_id


def delete_primary_event(access_token: str, *, event_id: str) -> str:
    """Hard-delete one Google Calendar event the creator already owns.

    Must only be called by an approved action executor after explicit
    creator confirmation. Returns the event id that was deleted (for
    logging / success-message use).
    """
    clean_event_id = _clean_event_id(event_id)
    url = f"{EVENTS_URL}/{clean_event_id}"
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = httpx.delete(url, headers=headers, timeout=20.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google Calendar events.delete failed with status %s", status)
        raise GoogleCalendarError("Google Calendar event delete failed") from exc
    return clean_event_id


def _clean_event_id(value: str) -> str:
    """Defensive guard against junk event ids — never substitutes a
    fallback, always raises so the staging layer surfaces the error
    to the creator before any Google call. Google event IDs are
    base32hex-ish (lowercase letters + digits + underscore + dash)."""
    raw = str(value or "").strip()
    if not raw:
        raise GoogleCalendarError("Google Calendar event id missing")
    if len(raw) > 1024:
        raise GoogleCalendarError("Google Calendar event id too long")
    # Whitespace and path separators are never valid in event ids.
    if any(ch in raw for ch in (" ", "/", "?", "#")):
        raise GoogleCalendarError("Google Calendar event id contains invalid characters")
    return raw


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


def _clean_datetime(value: str | None) -> str:
    raw = str(value or "").strip()[:64]
    if not raw:
        return ""
    # Accept the app's common datetime-local shape and normalize to an
    # explicit UTC timestamp for Google Calendar.
    candidate = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise GoogleCalendarError("Google Calendar event datetime invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _default_end(starts_at: str) -> str:
    try:
        parsed = datetime.fromisoformat(starts_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GoogleCalendarError("Google Calendar event datetime invalid") from exc
    return (parsed + timedelta(hours=1)).astimezone(UTC).isoformat().replace(
        "+00:00", "Z"
    )


def _google_dt(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
