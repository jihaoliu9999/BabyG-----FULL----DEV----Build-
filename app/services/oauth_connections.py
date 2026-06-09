"""Server-side OAuth connection storage.

Tokens stay in Supabase and are only read from trusted server routes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from postgrest.exceptions import APIError as PostgrestAPIError

from app.config import get_settings
from app.core import supabase_client
from app.integrations import google_calendar

logger = logging.getLogger(__name__)

PROVIDER_GOOGLE = "google"
GOOGLE_SERVICE_CALENDAR = "calendar"
GOOGLE_SERVICE_GMAIL = "gmail"
GOOGLE_SERVICES = (GOOGLE_SERVICE_CALENDAR, GOOGLE_SERVICE_GMAIL)
STATE_MAX_AGE = 60 * 10


def create_google_state(
    user_id: str,
    *,
    services: list[str] | None = None,
    scopes: list[str] | None = None,
    next_path: str = "/creator/calendar",
) -> str:
    selected_services = normalize_google_services(services or [GOOGLE_SERVICE_CALENDAR])
    selected_scopes = scopes or google_calendar.scopes_for_services(selected_services)
    return _state_serializer().dumps(
        {
            "user_id": user_id,
            "provider": PROVIDER_GOOGLE,
            "next": next_path,
            "services": selected_services,
            "scopes": selected_scopes,
        }
    )


def verify_google_state(state: str) -> dict[str, Any] | None:
    try:
        data = _state_serializer().loads(state, max_age=STATE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("provider") != PROVIDER_GOOGLE or not data.get("user_id"):
        return None
    return {
        "user_id": str(data["user_id"]),
        "next": str(data.get("next") or "/creator/calendar"),
        "services": normalize_google_services(data.get("services")),
        "scopes": _clean_scopes(data.get("scopes")),
    }


def normalize_google_services(value: Any) -> list[str]:
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.replace(",", " ").split()]
    elif isinstance(value, list | tuple | set):
        raw_values = [str(part).strip() for part in value]
    else:
        raw_values = []
    return [service for service in GOOGLE_SERVICES if service in raw_values]


def get_google_connection(user_id: str) -> dict[str, Any] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("oauth_connections")
            .select("*")
            .eq("user_id", user_id)
            .eq("provider", PROVIDER_GOOGLE)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("oauth connection lookup failed: %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def save_google_connection(
    user_id: str,
    token_response: dict[str, Any],
    *,
    requested_scopes: list[str] | None = None,
) -> bool:
    access_token = str(token_response.get("access_token") or "")
    if not access_token:
        return False
    existing = get_google_connection(user_id)
    refresh_token = token_response.get("refresh_token") or (
        existing or {}
    ).get("refresh_token")
    expires_at = _expires_at(token_response.get("expires_in"))
    granted_scopes = _scopes_for_save(
        token_response,
        existing=existing,
        requested_scopes=requested_scopes,
    )
    payload = {
        "user_id": user_id,
        "provider": PROVIDER_GOOGLE,
        "scopes": granted_scopes,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "provider_account_id": None,
    }
    try:
        supabase_client.get_service_client().table("oauth_connections").upsert(
            payload,
            on_conflict="user_id,provider",
        ).execute()
    except PostgrestAPIError:
        logger.exception("oauth connection save failed: %s", user_id)
        return False
    return True


def google_connection_scopes(connection: dict[str, Any] | None) -> list[str]:
    if not connection:
        return []
    return _clean_scopes(connection.get("scopes"))


def google_calendar_connected(connection: dict[str, Any] | None) -> bool:
    return google_calendar.has_calendar_scope(google_connection_scopes(connection))


def google_gmail_connected(connection: dict[str, Any] | None) -> bool:
    return google_calendar.has_gmail_scope(google_connection_scopes(connection))


def google_service_scopes(service: str) -> list[str]:
    if service == GOOGLE_SERVICE_CALENDAR:
        return [google_calendar.CALENDAR_SCOPE]
    if service == GOOGLE_SERVICE_GMAIL:
        return [
            scope
            for scope in google_calendar.scopes_for_services([GOOGLE_SERVICE_GMAIL])
            if google_calendar.is_gmail_scope(scope)
        ]
    return []


def remove_google_service(user_id: str, service: str) -> bool:
    connection = get_google_connection(user_id)
    if not connection:
        return True
    current_scopes = google_connection_scopes(connection)
    if service == GOOGLE_SERVICE_CALENDAR:
        remaining = [
            scope for scope in current_scopes if not google_calendar.is_calendar_scope(scope)
        ]
    elif service == GOOGLE_SERVICE_GMAIL:
        remaining = [
            scope for scope in current_scopes if not google_calendar.is_gmail_scope(scope)
        ]
    else:
        return False
    if not remaining:
        return disconnect_google(user_id)
    return _update_google_scopes(user_id, remaining)


def disconnect_google(user_id: str) -> bool:
    try:
        supabase_client.get_service_client().table("oauth_connections").delete().eq(
            "user_id", user_id
        ).eq("provider", PROVIDER_GOOGLE).execute()
    except PostgrestAPIError:
        logger.exception("oauth connection delete failed: %s", user_id)
        return False
    return True


def access_token_for_google(user_id: str) -> str | None:
    connection = get_google_connection(user_id)
    if not connection:
        return None
    access_token = str(connection.get("access_token") or "")
    refresh_token = str(connection.get("refresh_token") or "")
    if access_token and not _is_expired(connection.get("expires_at")):
        return access_token
    if not refresh_token:
        return access_token or None
    try:
        refreshed = google_calendar.refresh_access_token(refresh_token)
    except google_calendar.GoogleCalendarError:
        logger.info("Google token refresh failed for user %s", user_id)
        return access_token or None
    if save_google_connection(user_id, refreshed):
        return str(refreshed.get("access_token") or access_token)
    return access_token or None


def _update_google_scopes(user_id: str, scopes: list[str]) -> bool:
    try:
        supabase_client.get_service_client().table("oauth_connections").update(
            {"scopes": scopes}
        ).eq("user_id", user_id).eq("provider", PROVIDER_GOOGLE).execute()
    except PostgrestAPIError:
        logger.exception("oauth connection scope update failed: %s", user_id)
        return False
    return True


def _scopes_for_save(
    token_response: dict[str, Any],
    *,
    existing: dict[str, Any] | None,
    requested_scopes: list[str] | None,
) -> list[str]:
    explicit = _clean_scopes(token_response.get("scope"))
    if explicit:
        return explicit
    existing_scopes = google_connection_scopes(existing)
    if requested_scopes is not None:
        return _dedupe([*existing_scopes, *_clean_scopes(requested_scopes)])
    if existing_scopes:
        return existing_scopes
    return google_calendar.scopes()


def _clean_scopes(value: Any) -> list[str]:
    if isinstance(value, str):
        raw = value.replace(",", " ").split()
    elif isinstance(value, list | tuple | set):
        raw = [str(item) for item in value]
    else:
        raw = []
    return _dedupe([scope.strip() for scope in raw if scope and scope.strip()])


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _state_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(
        get_settings().session_secret, salt="bg.google.oauth.v1"
    )


def _expires_at(expires_in: Any) -> str | None:
    try:
        seconds = int(expires_in)
    except (TypeError, ValueError):
        return None
    return (datetime.now(UTC) + timedelta(seconds=max(seconds, 0))).isoformat()


def _is_expired(value: Any) -> bool:
    if not value:
        return False
    raw = str(value).replace("Z", "+00:00")
    try:
        expires_at = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    return expires_at <= datetime.now(UTC) + timedelta(seconds=60)
