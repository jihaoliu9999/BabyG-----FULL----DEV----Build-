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
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
CALENDAR_LIST_URL = "https://www.googleapis.com/calendar/v3/users/me/calendarList"
EVENTS_BASE_URL = "https://www.googleapis.com/calendar/v3/calendars"
EVENTS_URL = f"{EVENTS_BASE_URL}/primary/events"
DEFAULT_CALLBACK_PATH = "/creator/google/calendar/callback"
CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.events"
# Legacy: pre-Slice-2 Gmail connections used the read-only scope. Kept
# as a named constant so existing-user detection (and back-compat tests)
# can still reference it. New Gmail connections request COMPOSE which
# is required for drafts.create; READONLY is a strict subset of what
# COMPOSE grants.
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_COMPOSE_SCOPE = "https://www.googleapis.com/auth/gmail.compose"
GMAIL_SEND_SCOPE = "https://www.googleapis.com/auth/gmail.send"
GMAIL_SCOPE_PREFIX = "https://www.googleapis.com/auth/gmail."


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
    return [CALENDAR_SCOPE]


def scopes_for_services(services: list[str]) -> list[str]:
    configured = allowed_scopes(scopes())
    selected: list[str] = []
    if "calendar" in services:
        selected.extend([scope for scope in configured if is_calendar_scope(scope)])
        if not selected:
            selected.append(CALENDAR_SCOPE)
    if "gmail" in services:
        gmail_scopes = [scope for scope in configured if is_gmail_scope(scope)]
        # New Gmail connections grant COMPOSE for approved drafts and
        # SEND for approved one-off sends. The action proposal system
        # remains the runtime gate for every external write.
        selected.extend(
            gmail_scopes
            or [GMAIL_READONLY_SCOPE, GMAIL_COMPOSE_SCOPE, GMAIL_SEND_SCOPE]
        )
    return _dedupe(selected)


def auth_url(state: str, *, scopes_override: list[str] | None = None) -> str:
    if not is_configured():
        raise GoogleCalendarError("Google OAuth is not configured")
    selected_scopes = scopes_override or scopes()
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


def is_gmail_read_scope(scope: str) -> bool:
    return scope == GMAIL_READONLY_SCOPE


def allowed_scopes(scopes_to_check: list[str] | set[str] | tuple[str, ...]) -> list[str]:
    allowed = {
        CALENDAR_SCOPE,
        GMAIL_READONLY_SCOPE,
        GMAIL_COMPOSE_SCOPE,
        GMAIL_SEND_SCOPE,
    }
    return _dedupe([scope for scope in scopes_to_check if scope in allowed])


def is_gmail_compose_scope(scope: str) -> bool:
    return scope == GMAIL_COMPOSE_SCOPE


def is_gmail_send_scope(scope: str) -> bool:
    return scope == GMAIL_SEND_SCOPE


def has_calendar_scope(scopes_to_check: list[str] | set[str] | tuple[str, ...]) -> bool:
    return any(is_calendar_scope(scope) for scope in scopes_to_check)


def has_gmail_compose_scope(
    scopes_to_check: list[str] | set[str] | tuple[str, ...],
) -> bool:
    """True only when the compose scope is present. Read-only connections
    return False — they need to reconnect before drafts can be staged."""
    return any(is_gmail_compose_scope(scope) for scope in scopes_to_check)


def has_gmail_read_scope(
    scopes_to_check: list[str] | set[str] | tuple[str, ...],
) -> bool:
    return any(is_gmail_read_scope(scope) for scope in scopes_to_check)


def has_gmail_send_scope(
    scopes_to_check: list[str] | set[str] | tuple[str, ...],
) -> bool:
    """True only when gmail.send is present. Compose-only connections
    can draft but must reconnect before approved sends are available."""
    return any(is_gmail_send_scope(scope) for scope in scopes_to_check)


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


def revoke_token(token: str) -> bool:
    clean_token = str(token or "").strip()
    if not clean_token:
        return False
    try:
        response = httpx.post(REVOKE_URL, data={"token": clean_token}, timeout=20.0)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google OAuth token revoke failed with status %s", status)
        raise GoogleCalendarError("Google OAuth token revoke failed") from exc
    return True


def list_calendar_ids(access_token: str) -> list[str]:
    """Return accessible calendar ids, falling back to primary.

    Some existing connections only have the narrower calendar.events
    scope. That scope can still read/write events on calendars the user
    granted, but may be refused by calendarList. A refusal should not
    break sync; it means we sync the primary calendar this connection can
    already address.
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    params = {"minAccessRole": "reader", "showHidden": "false", "maxResults": "250"}
    calendars: list[str] = []
    page_token: str | None = None
    while True:
        request_params = dict(params)
        if page_token:
            request_params["pageToken"] = page_token
        try:
            response = httpx.get(
                CALENDAR_LIST_URL,
                params=request_params,
                headers=headers,
                timeout=20.0,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status = getattr(exc.response, "status_code", "?")
            logger.info("Google Calendar calendarList.list failed with status %s", status)
            return ["primary"]
        except httpx.HTTPError as exc:
            status = getattr(getattr(exc, "response", None), "status_code", "?")
            logger.info("Google Calendar calendarList.list failed with status %s", status)
            raise GoogleCalendarError("Google Calendar list request failed") from exc
        data = response.json()
        for item in data.get("items", []) if isinstance(data, dict) else []:
            calendar_id = str((item or {}).get("id") or "").strip()
            if calendar_id:
                calendars.append(calendar_id)
        page_token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
        if not page_token:
            break
    return _dedupe(calendars) or ["primary"]


def list_events(
    access_token: str,
    *,
    calendar_id: str = "primary",
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
        "showDeleted": "true",
    }
    headers = {"Authorization": f"Bearer {access_token}"}
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    url = f"{EVENTS_BASE_URL}/{calendar_id}/events"
    try:
        while True:
            request_params = dict(params)
            if page_token:
                request_params["pageToken"] = page_token
            response = httpx.get(url, params=request_params, headers=headers, timeout=20.0)
            response.raise_for_status()
            data = response.json()
            page_items = data.get("items", []) if isinstance(data, dict) else []
            if isinstance(page_items, list):
                items.extend(item for item in page_items if isinstance(item, dict))
            page_token = str(data.get("nextPageToken") or "") if isinstance(data, dict) else ""
            if not page_token:
                break
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Google Calendar events.list failed with status %s", status)
        raise GoogleCalendarError("Google Calendar events request failed") from exc
    for item in items:
        item.setdefault("_babyg_calendar_id", calendar_id)
    return items


def list_primary_events(
    access_token: str,
    *,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    max_results: int = 100,
) -> list[dict[str, Any]]:
    return list_events(
        access_token,
        calendar_id="primary",
        time_min=time_min,
        time_max=time_max,
        max_results=max_results,
    )


def create_primary_event(
    access_token: str,
    *,
    title: str,
    starts_at: str,
    ends_at: str | None = None,
    notes: str | None = None,
    location: str | None = None,
    visibility: str | None = None,
    transparency: str | None = None,
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
    clean_visibility = str(visibility or "").strip()
    clean_transparency = str(transparency or "").strip()
    if clean_visibility:
        payload["visibility"] = clean_visibility
    if clean_transparency:
        payload["transparency"] = clean_transparency

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
    if not event_id:
        return None

    start_obj = event.get("start") or {}
    end_obj = event.get("end") or {}
    start = _event_time(start_obj)
    if not start:
        return None
    end = _event_time(end_obj)
    summary = str(event.get("summary") or "untitled event").strip()[:140]
    location = str(event.get("location") or "").strip()[:160]
    description = str(event.get("description") or "").strip()[:2000]
    calendar_id = str(event.get("_babyg_calendar_id") or "primary").strip()[:300]

    return {
        "title": summary or "untitled event",
        "type": "event",
        "starts_at": start,
        "ends_at": end,
        "notes": description or None,
        "status": "cancelled" if event.get("status") == "cancelled" else "confirmed",
        "venue_name": location or None,
        "google_calendar_id": calendar_id or "primary",
        "google_event_id": event_id,
        "is_all_day": "date" in start_obj,
        "google_timezone": _event_timezone(start_obj, end_obj),
        "google_recurring_event_id": str(event.get("recurringEventId") or "").strip() or None,
        "google_original_start_time": event.get("originalStartTime") or None,
        "google_status": str(event.get("status") or "").strip()[:40] or None,
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


def _event_timezone(start: dict[str, Any], end: dict[str, Any]) -> str | None:
    for value in (start, end):
        if not isinstance(value, dict):
            continue
        tz = str(value.get("timeZone") or "").strip()
        if tz:
            return tz[:80]
    return None


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
