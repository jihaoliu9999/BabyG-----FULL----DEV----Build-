"""CSRF middleware behavior.

The autouse fixture in conftest disables CSRF for the rest of the suite.
Here we re-enable it (via a fresh TestClient with no monkeypatch) and
exercise the cases that matter:

  * POST without a token is rejected
  * POST with a valid token (minted via a prior GET that scrapes the
    hidden input) is accepted
  * POST with a foreign Origin header is rejected even with a token
  * GETs are never blocked
  * The /auth/callback exempt path is not blocked
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

# Capture the real dispatch at import time, before conftest's autouse
# `_csrf_off` patches it. Tests in this file restore it via `csrf_client`.
from app.core import csrf as _csrf_module

_REAL_CALL = _csrf_module.CSRFMiddleware.__call__

from app.main import app  # noqa: E402  (import order matters: snapshot first)


@pytest.fixture()
def csrf_client(monkeypatch) -> TestClient:
    monkeypatch.setattr(_csrf_module.CSRFMiddleware, "__call__", _REAL_CALL)
    return TestClient(app)


_TOKEN_RE = re.compile(r'name="csrf_token"\s+value="([^"]+)"')


def _scrape_token(client: TestClient, path: str) -> str:
    resp = client.get(path)
    assert resp.status_code == 200, resp.status_code
    match = _TOKEN_RE.search(resp.text)
    assert match, "no csrf_token rendered on " + path
    return match.group(1)


def test_post_without_token_is_rejected(csrf_client):
    r = csrf_client.post(
        "/auth/magic-link", data={"email": "a@b.example", "role": "creator"}
    )
    assert r.status_code == 403


def test_post_with_valid_token_is_accepted(csrf_client, monkeypatch):
    # Stub the supabase OTP send so we don't network out.
    from types import SimpleNamespace

    from app.core import supabase_client

    monkeypatch.setattr(
        supabase_client,
        "get_anon_client",
        lambda: SimpleNamespace(auth=SimpleNamespace(sign_in_with_otp=lambda _: None)),
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

    token = _scrape_token(csrf_client, "/auth/login?role=creator")
    r = csrf_client.post(
        "/auth/magic-link",
        data={"email": "a@b.example", "role": "creator", "csrf_token": token},
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
