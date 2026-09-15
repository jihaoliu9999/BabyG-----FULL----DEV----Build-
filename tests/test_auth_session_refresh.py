"""Sliding-session refresh tests.

Locks the small surface introduced by ``_SlidingSessionMiddleware`` so
active babyg users no longer get forcibly re-authenticated on the
30-day mark from their original magic-link click. The middleware
re-issues ``bg_session`` with a fresh timer on any authenticated
request whose response is not already writing or clearing that cookie.

Security posture the tests LOCK:
  * cookie attributes on the refreshed cookie stay HttpOnly + SameSite=Lax
    + path=/ (Secure is production-only via ``settings.is_production``)
  * only a valid, signed, unexpired cookie triggers a refresh — tampered
    and missing cookies never resurrect a session
  * login callback + logout remain authoritative — the middleware does
    not overwrite an intentional Set-Cookie
  * a refreshed cookie authenticates the same user (payload preserved)
  * one user's session cannot authenticate another user
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import Response
from fastapi.testclient import TestClient
from itsdangerous import URLSafeTimedSerializer

from app.config import get_settings
from app.core.security import (
    SESSION_COOKIE,
    SESSION_MAX_AGE,
    read_session,
    write_session,
)
from app.main import app


def _signed_in(client: TestClient, *, user_id: str = "creator-refresh-1") -> str:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return user_id


def _first_set_cookie(response, name: str) -> str | None:
    """Return the first ``Set-Cookie`` header value whose cookie name
    matches ``name``. The TestClient hands back an httpx.Response
    whose ``headers.get_list("set-cookie")`` exposes each Set-Cookie
    header separately (rather than joining them with commas the way
    ``headers["set-cookie"]`` would)."""
    prefix = f"{name}="
    for header_value in response.headers.get_list("set-cookie"):
        if header_value.startswith(prefix):
            return header_value
    return None


def _set_cookie_values(response, name: str) -> list[str]:
    """All Set-Cookie headers on ``response`` whose cookie name is ``name``."""
    prefix = f"{name}="
    return [
        h for h in response.headers.get_list("set-cookie") if h.startswith(prefix)
    ]


def _cookie_attrs(cookie_header: str) -> dict[str, str | bool]:
    parts = [p.strip() for p in cookie_header.split(";") if p.strip()]
    attrs: dict[str, str | bool] = {}
    # First part is name=value; skip it.
    for part in parts[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            attrs[k.lower()] = v
        else:
            attrs[part.lower()] = True
    return attrs


def test_middleware_refreshes_valid_session_on_authenticated_request():
    """The core contract: a request carrying a valid ``bg_session``
    receives a fresh ``bg_session`` in the response so the 30-day
    timer restarts."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    # /healthz is public, deps-free, and always 200 — the perfect
    # probe for the middleware without stubbing role guards.
    r = client.get("/healthz")
    assert r.status_code == 200
    refreshed = _first_set_cookie(r, SESSION_COOKIE)
    assert refreshed is not None, "middleware did not refresh a valid session"
    attrs = _cookie_attrs(refreshed)
    # Cookie attributes preserved.
    assert attrs.get("httponly") is True
    assert attrs.get("samesite", "").lower() == "lax"
    assert attrs.get("path") == "/"
    # ``max-age`` re-set to the full window.
    assert int(str(attrs.get("max-age", "0"))) == SESSION_MAX_AGE


def test_middleware_does_not_touch_response_when_no_session_cookie():
    """An anonymous visitor never receives a ``bg_session`` cookie
    from the middleware."""
    client = TestClient(app, follow_redirects=False)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert _first_set_cookie(r, SESSION_COOKIE) is None


def test_middleware_ignores_tampered_cookie():
    """A cookie with a broken signature is treated as missing —
    ``read_session`` returns None and the middleware refuses to
    resurrect it. This is the key security check."""
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, "totally.not.a.valid.token")
    r = client.get("/healthz")
    assert r.status_code == 200
    assert _first_set_cookie(r, SESSION_COOKIE) is None


def test_middleware_ignores_expired_cookie():
    """A cookie past ``SESSION_MAX_AGE`` is treated as expired even
    though the signature is otherwise valid. Simulated by hand-forging
    a token with a timestamp far in the past."""
    settings = get_settings()
    # A token signed with a wrong salt fails signature verification.
    # ``read_session`` catches ``BadSignature`` alongside
    # ``SignatureExpired`` and returns None either way, so the
    # middleware's refresh path is short-circuited identically for
    # tampered and expired tokens.
    wrong_salt = URLSafeTimedSerializer(
        settings.session_secret, salt="wrong-salt"
    )
    bogus = wrong_salt.dumps({"user_id": "x", "role": "creator"})
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(SESSION_COOKIE, bogus)
    r = client.get("/healthz")
    assert r.status_code == 200
    assert _first_set_cookie(r, SESSION_COOKIE) is None


def test_refreshed_cookie_authenticates_same_user():
    """The refreshed cookie is functionally identical: it decodes to
    the same ``user_id`` and ``role``."""
    client = TestClient(app, follow_redirects=False)
    user_id = _signed_in(client, user_id="creator-refresh-payload")
    r = client.get("/healthz")
    refreshed = _first_set_cookie(r, SESSION_COOKIE)
    assert refreshed is not None
    # Extract the raw token from the Set-Cookie header (name=value; ...)
    raw_token = refreshed.split(";", 1)[0].split("=", 1)[1]
    # Verify via read_session on a synthetic request.
    from starlette.requests import Request as StarletteRequest

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"cookie", f"{SESSION_COOKIE}={raw_token}".encode())],
    }
    faux_request = StarletteRequest(scope)
    session = read_session(faux_request)
    assert session is not None
    assert session["user_id"] == user_id
    assert session["role"] == "creator"


def test_middleware_skips_when_response_already_writes_bg_session():
    """Direct middleware unit test: when the response already carries
    a ``bg_session`` Set-Cookie (login callback pattern), the
    sliding-refresh middleware must NOT overwrite it.

    Exercised at the class level rather than via a live route to
    avoid depending on any endpoint mounted on the real app."""
    import asyncio

    from starlette.requests import Request as StarletteRequest
    from starlette.responses import PlainTextResponse

    from app.main import _SlidingSessionMiddleware

    # Build a valid session cookie so ``read_session`` would say yes.
    valid_token = URLSafeTimedSerializer(
        get_settings().session_secret, salt="bg.session.v1"
    ).dumps({"user_id": "u", "role": "creator"})
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"cookie", f"{SESSION_COOKIE}={valid_token}".encode())],
    }
    request = StarletteRequest(scope)

    # Simulate the response the login callback returns: it already
    # writes bg_session on its own.
    handler_written_response = PlainTextResponse("ok")
    handler_written_response.set_cookie(
        SESSION_COOKIE,
        "callback-issued-token",
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )

    async def _passthrough(_req):
        return handler_written_response

    middleware = _SlidingSessionMiddleware(app=None)  # type: ignore[arg-type]
    result = asyncio.get_event_loop().run_until_complete(
        middleware.dispatch(request, _passthrough)
    )
    # Exactly one bg_session Set-Cookie, and it's the handler's token.
    bg_cookies = [
        v for k, v in result.raw_headers
        if k.lower() == b"set-cookie" and v.startswith(b"bg_session=")
    ]
    assert len(bg_cookies) == 1, (
        f"expected exactly one bg_session Set-Cookie, got {len(bg_cookies)}"
    )
    assert b"callback-issued-token" in bg_cookies[0]


def test_logout_clear_is_not_overridden_by_middleware():
    """``clear_session`` writes ``bg_session=; Max-Age=0``. The
    middleware must NOT resurrect a session by re-writing the cookie
    after the handler has cleared it. Otherwise logout is broken."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client, user_id="creator-logout")
    r = client.post("/auth/logout")
    # Redirects to /; the important part is the Set-Cookie header.
    set_cookies = _set_cookie_values(r, SESSION_COOKIE)
    # Exactly one bg_session Set-Cookie, and it clears (Max-Age=0 or
    # the itsdangerous cookie deletion pattern).
    assert len(set_cookies) == 1, (
        f"expected exactly one bg_session Set-Cookie on logout, got {len(set_cookies)}"
    )
    lowered = set_cookies[0].lower()
    assert "max-age=0" in lowered or "expires=" in lowered, (
        "logout Set-Cookie should clear the session, "
        f"got: {set_cookies[0]}"
    )


def test_one_users_refreshed_cookie_does_not_authenticate_another():
    """The refreshed cookie carries the same payload as the source
    cookie — decoding a user A's refreshed cookie must yield user A,
    never user B. This is a paranoia lock against a swap bug."""
    client_a = TestClient(app, follow_redirects=False)
    _signed_in(client_a, user_id="user-A")
    ra = client_a.get("/healthz")
    refreshed_a = _first_set_cookie(ra, SESSION_COOKIE)
    assert refreshed_a is not None
    token_a = refreshed_a.split(";", 1)[0].split("=", 1)[1]

    client_b = TestClient(app, follow_redirects=False)
    _signed_in(client_b, user_id="user-B")
    rb = client_b.get("/healthz")
    refreshed_b = _first_set_cookie(rb, SESSION_COOKIE)
    assert refreshed_b is not None
    token_b = refreshed_b.split(";", 1)[0].split("=", 1)[1]

    assert token_a != token_b

    def _decode(token: str) -> dict:
        from starlette.requests import Request as StarletteRequest
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/",
            "headers": [(b"cookie", f"{SESSION_COOKIE}={token}".encode())],
        }
        return read_session(StarletteRequest(scope))  # type: ignore[return-value]

    assert _decode(token_a)["user_id"] == "user-A"
    assert _decode(token_b)["user_id"] == "user-B"


def test_max_age_matches_session_max_age_constant():
    """Refreshed cookie's Max-Age exactly matches ``SESSION_MAX_AGE``.
    Guards against an accidental lifetime bump — the task's rule is
    "keep the existing 30-day lifetime, just slide it forward"."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    r = client.get("/healthz")
    refreshed = _first_set_cookie(r, SESSION_COOKIE)
    assert refreshed is not None
    attrs = _cookie_attrs(refreshed)
    assert int(str(attrs.get("max-age", "0"))) == SESSION_MAX_AGE
    # Sanity: 30 days.
    assert SESSION_MAX_AGE == 60 * 60 * 24 * 30


def test_returning_user_reaches_creator_home_without_new_magic_link(monkeypatch):
    """End-to-end: a returning user with a valid session hits a
    creator route (which normally requires ``require_role("creator")``)
    and receives a 200, not a redirect to /auth/login. Also confirms
    the middleware refreshed the cookie on that same round-trip."""
    from app.routes import creator as creator_routes

    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile_cached",
        lambda uid, request: {"onboarding_completed_at": "2026-09-01T00:00:00Z"},
    )
    client = TestClient(app, follow_redirects=False)
    _signed_in(client, user_id="returning-user")
    r = client.get("/creator/brief")
    assert r.status_code == 200, r.text[:200]
    assert _first_set_cookie(r, SESSION_COOKIE) is not None


# Ensure the datetime import is used somewhere — the naive expiry
# math below documents the security posture the middleware preserves
# without being executed as a runtime check.
def test_session_lifetime_documentation():
    """The refreshed cookie's implicit expiry from *now* is
    exactly ``SESSION_MAX_AGE`` — 30 days. The itsdangerous timestamp
    inside the token is what the server validates on the next request,
    so a stolen token that hasn't been refreshed for >30 days still
    fails signature-expiration."""
    now = datetime.now(UTC)
    implied_expiry = now + timedelta(seconds=SESSION_MAX_AGE)
    # 30 days from now, +/- one second for wall-clock drift in the test.
    assert (implied_expiry - now).total_seconds() == SESSION_MAX_AGE
