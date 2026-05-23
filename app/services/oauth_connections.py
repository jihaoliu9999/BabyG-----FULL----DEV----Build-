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
STATE_MAX_AGE = 60 * 10


def create_google_state(user_id: str, *, next_path: str = "/creator/calendar") -> str:
    return _state_serializer().dumps(
        {"user_id": user_id, "provider": PROVIDER_GOOGLE, "next": next_path}
    )


def verify_google_state(state: str) -> dict[str, str] | None:
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
    }


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


def save_google_connection(user_id: str, token_response: dict[str, Any]) -> bool:
    access_token = str(token_response.get("access_token") or "")
    if not access_token:
        return False
    existing = get_google_connection(user_id)
    refresh_token = token_response.get("refresh_token") or (
        existing or {}
    ).get("refresh_token")
    expires_at = _expires_at(token_response.get("expires_in"))
    scope_text = str(token_response.get("scope") or " ".join(google_calendar.scopes()))
    payload = {
        "user_id": user_id,
        "provider": PROVIDER_GOOGLE,
        "scopes": [s for s in scope_text.split() if s],
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
