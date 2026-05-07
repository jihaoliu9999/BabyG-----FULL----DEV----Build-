"""Signed session cookies via itsdangerous.

We do not use Starlette's SessionMiddleware because we want to keep the cookie
payload small (just user_id + role) and have explicit, testable encode/decode
helpers. The cookie is HttpOnly + Secure (in prod) + SameSite=Lax.

Two serializers live here:
  * the long-lived session serializer (30 days) — cookie name `bg_session`
  * a short-lived "pending magic-link" serializer (10 minutes) — cookie name
    `bg_pending_role`, used to remember which role the user requested before
    they click the email link
"""

from __future__ import annotations

from typing import TypedDict

from fastapi import Request, Response
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import get_settings

SESSION_COOKIE = "bg_session"
PENDING_ROLE_COOKIE = "bg_pending_role"

SESSION_MAX_AGE = 60 * 60 * 24 * 30      # 30 days
PENDING_ROLE_MAX_AGE = 60 * 10           # 10 minutes


class SessionPayload(TypedDict):
    user_id: str
    role: str


def _session_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="bg.session.v1")


def _pending_serializer() -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(get_settings().session_secret, salt="bg.pending.v1")


def _cookie_kwargs() -> dict:
    settings = get_settings()
    return {
        "httponly": True,
        "secure": settings.is_production,
        "samesite": "lax",
        "path": "/",
    }


def write_session(response: Response, payload: SessionPayload) -> None:
    token = _session_serializer().dumps(dict(payload))
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_MAX_AGE, **_cookie_kwargs())


def read_session(request: Request) -> SessionPayload | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    try:
        data = _session_serializer().loads(token, max_age=SESSION_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict) or "user_id" not in data or "role" not in data:
        return None
    return SessionPayload(user_id=str(data["user_id"]), role=str(data["role"]))


def clear_session(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/")


def write_pending_role(response: Response, role: str) -> None:
    token = _pending_serializer().dumps({"role": role})
    response.set_cookie(
        PENDING_ROLE_COOKIE, token, max_age=PENDING_ROLE_MAX_AGE, **_cookie_kwargs()
    )


def read_pending_role(request: Request) -> str | None:
    token = request.cookies.get(PENDING_ROLE_COOKIE)
    if not token:
        return None
    try:
        data = _pending_serializer().loads(token, max_age=PENDING_ROLE_MAX_AGE)
    except (BadSignature, SignatureExpired):
        return None
    role = data.get("role") if isinstance(data, dict) else None
    return role if isinstance(role, str) else None


def clear_pending_role(response: Response) -> None:
    response.delete_cookie(PENDING_ROLE_COOKIE, path="/")
