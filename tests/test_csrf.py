"""CSRF middleware behavior.

The autouse fixture in conftest disables CSRF for the rest of the suite.
Here we re-enable it (via a fresh TestClient with no monkeypatch) and
exercise the cases that matter:

  * POST without a token is rejected
  * POST with a valid token (minted via a prior GET that scrapes the
    hidden input) is accepted
  * POST with a foreign Origin header is rejected even with a token
  * POST with NO Origin/Referer at all is rejected (default-deny — modern
    browsers always send one on state-changing requests)
  * Rejection content-negotiates: HTML clients get a readable page,
    JSON clients get `{"detail": ...}`
  * Bodies above the size cap are 413'd before any route logic runs
  * GETs are never blocked
  * The /auth/callback exempt path is not blocked
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

# Capture the real dispatch at import time, before conftest's autouse
# `_csrf_off` patches it. Tests in this file restore it via `csrf_client`.
from app.core import csrf as _csrf_module

_REAL_CALL = _csrf_module.CSRFMiddleware.__call__

from app.core.security import SESSION_COOKIE, write_session  # noqa: E402
from app.main import app  # noqa: E402  (import order matters: snapshot first)

# Origin that matches `request.url.netloc` so _origin_ok approves the
# request. Without this header, the strict-Origin policy rejects POSTs.
SAME_ORIGIN = "http://testserver"


@pytest.fixture()
def csrf_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(_csrf_module.CSRFMiddleware, "__call__", _REAL_CALL)
    return TestClient(app)


@pytest.fixture()
def stub_supabase(monkeypatch):
    """Replace both Supabase clients with no-op stubs so the magic-link
    route runs end-to-end without env vars or network."""
    from app.core import supabase_client

    monkeypatch.setattr(
        supabase_client,
        "get_anon_client",
        lambda: SimpleNamespace(
            auth=SimpleNamespace(sign_in_with_otp=lambda _a: None)
        ),
    )
    monkeypatch.setattr(
        supabase_client,
        "get_service_client",
        lambda: SimpleNamespace(
            table=lambda *_: SimpleNamespace(
                select=lambda *_a, **_k: SimpleNamespace(
                    eq=lambda *_a, **_k: SimpleNamespace(
                        eq=lambda *_a, **_k: SimpleNamespace(
                            limit=lambda *_a, **_k: SimpleNamespace(
                                execute=lambda: SimpleNamespace(data=[])
                            )
                        )
                    )
                )
            )
        ),
    )


_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def _scrape_token(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    match = _TOKEN_RE.search(resp.text)
    assert match, "no csrf_token rendered on " + path
    return match.group(1)


def test_post_without_token_is_rejected(csrf_client):
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator"},
        headers={"origin": SAME_ORIGIN},
    )
    assert r.status_code == 403


def test_account_deletion_guard_keeps_csrf_protection(csrf_client, monkeypatch):
    from app.routes import creator as creator_routes

    cookie_response = Response()
    write_session(cookie_response, {"user_id": "creator-1", "role": "creator"})
    cookie = cookie_response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    csrf_client.cookies.set(SESSION_COOKIE, cookie, domain="testserver.local", path="/")
    monkeypatch.setattr(
        creator_routes.profiles,
        "delete_account",
        lambda _uid: pytest.fail("deletion service invoked"),
    )

    missing = csrf_client.post(
        "/creator/profile/delete",
        data={"confirm": "delete"},
        headers={"origin": SAME_ORIGIN},
        follow_redirects=False,
    )
    assert missing.status_code == 403

    invalid = csrf_client.post(
        "/creator/profile/delete",
        data={"confirm": "delete", "csrf_token": "invalid"},
        headers={"origin": SAME_ORIGIN},
        follow_redirects=False,
    )
    assert invalid.status_code == 403

    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    refused = csrf_client.post(
        "/creator/profile/delete",
        data={"confirm": "delete", "csrf_token": token},
        headers={"origin": SAME_ORIGIN},
        follow_redirects=False,
    )
    assert refused.status_code == 303
    assert refused.headers["location"] == (
        "/creator/profile/settings?delete=unavailable#delete-account"
    )
    assert csrf_client.cookies.get(SESSION_COOKIE, domain="testserver.local", path="/") == cookie


def test_enabled_account_deletion_requires_csrf_and_same_origin(csrf_client, monkeypatch):
    from app.routes import creator as creator_routes
    from app.services import profiles

    monkeypatch.setenv("BABYG_ACCOUNT_DELETION_ENABLED", "true")
    creator_routes.get_settings.cache_clear()
    calls: list[str] = []

    def _delete(user_id: str) -> profiles.AccountDeletion:
        calls.append(user_id)
        return profiles.AccountDeletion("deleted")

    monkeypatch.setattr(creator_routes.profiles, "delete_account", _delete)
    cookie_response = Response()
    write_session(cookie_response, {"user_id": "creator-1", "role": "creator"})
    cookie = cookie_response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    csrf_client.cookies.set(SESSION_COOKIE, cookie, domain="testserver.local", path="/")

    for data in ({"confirm": "delete"}, {"confirm": "delete", "csrf_token": "invalid"}):
        rejected = csrf_client.post(
            "/creator/profile/delete",
            data=data,
            headers={"origin": SAME_ORIGIN},
            follow_redirects=False,
        )
        assert rejected.status_code == 403

    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    cross_site = csrf_client.post(
        "/creator/profile/delete",
        data={"confirm": "delete", "csrf_token": token},
        headers={"origin": "https://evil.example"},
        follow_redirects=False,
    )
    assert cross_site.status_code == 403
    assert calls == []

    accepted = csrf_client.post(
        "/creator/profile/delete",
        data={"confirm": "delete", "csrf_token": token},
        headers={"origin": SAME_ORIGIN},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert accepted.headers["location"] == "/data-deletion?deleted=1#account-deleted"
    assert calls == ["creator-1"]


def test_post_with_valid_token_is_accepted(csrf_client, stub_supabase):
    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator", "csrf_token": token},
        headers={"origin": SAME_ORIGIN},
    )
    assert r.status_code == 200


def test_post_with_foreign_origin_is_rejected(csrf_client):
    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator", "csrf_token": token},
        headers={"origin": "https://evil.example"},
    )
    assert r.status_code == 403


def test_post_with_missing_origin_and_referer_is_rejected(csrf_client):
    """Default-deny on missing Origin/Referer. Modern browsers always
    send Origin on POST; absence is suspicious."""
    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator", "csrf_token": token},
    )
    assert r.status_code == 403


def test_post_with_referer_only_is_accepted(csrf_client, stub_supabase):
    """Referer alone (no Origin) still satisfies the same-origin gate."""
    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator", "csrf_token": token},
        headers={"referer": SAME_ORIGIN + "/auth/login"},
    )
    assert r.status_code == 200


def test_rejection_renders_html_for_browser_clients(csrf_client):
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator"},
        headers={
            "origin": "https://evil.example",
            "accept": "text/html,application/xhtml+xml",
        },
    )
    assert r.status_code == 403
    assert "text/html" in r.headers.get("content-type", "")
    assert "<h1>" in r.text
    assert "{" not in r.text  # not JSON


def test_rejection_renders_json_for_api_clients(csrf_client):
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator"},
        headers={
            "origin": "https://evil.example",
            "accept": "application/json",
        },
    )
    assert r.status_code == 403
    assert "application/json" in r.headers.get("content-type", "")
    assert r.json() == {"detail": "csrf failed"}


def test_oversized_body_is_413(csrf_client):
    """A body larger than `MAX_CSRF_BODY_BYTES` short-circuits with 413
    instead of being buffered."""
    too_big = "x" * (_csrf_module.MAX_CSRF_BODY_BYTES + 100)
    r = csrf_client.post(
        "/auth/magic-link",
        # `data=` plus a giant string sends form-encoded; large enough
        # to trip the cap regardless of overhead.
        data={"email": "a@b.example", "role": "creator", "junk": too_big},
        headers={"origin": SAME_ORIGIN},
    )
    assert r.status_code == 413


def test_get_is_never_blocked(csrf_client):
    r = csrf_client.get("/auth/login?role=creator")
    assert r.status_code == 200


def test_auth_callback_is_exempt(csrf_client):
    # /auth/callback can arrive in a fresh tab with no session cookie and
    # no preceding GET; it must not require a CSRF token.
    r = csrf_client.get("/auth/callback?token_hash=x&type=magiclink")
    # We only care that the CSRF middleware didn't pre-empt with 403.
    # The actual handler may 400 on the (invalid) token — that's fine.
    assert r.status_code != 403
