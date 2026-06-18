"""Marketing-page routes.

Covers /, /get-started, /robots.txt, and the signed-in shortcut.
test_auth.py also exercises some of these — these tests are the
focused coverage for app/routes/marketing.py.
"""

from __future__ import annotations

import pytest
from fastapi import Response as _R
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def test_landing_renders_for_anon(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "babyg" in r.text.lower()


def test_landing_redirects_signed_in_creator(client: TestClient) -> None:
    resp = _R()
    write_session(resp, {"user_id": "u-creator", "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/creator"



def test_landing_redirects_signed_in_operator(client: TestClient) -> None:
    resp = _R()
    write_session(resp, {"user_id": "u-op", "role": "operator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    r = client.get("/")
    assert r.headers["location"] == "/operator"


def test_landing_redirects_signed_in_brand(client: TestClient) -> None:
    resp = _R()
    write_session(resp, {"user_id": "u-brand", "role": "brand"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    r = client.get("/")
    assert r.headers["location"] == "/brand"


def test_landing_handles_unknown_role_safely(client: TestClient) -> None:
    # If a malformed cookie ever surfaces a role we don't recognize, the
    # session reader should reject it, so this is anonymous behavior.
    client.cookies.set(SESSION_COOKIE, "not-a-real-token")
    r = client.get("/")
    # Anonymous fallback: render the landing page.
    assert r.status_code == 200


def test_get_started_renders_role_cards(client: TestClient) -> None:
    r = client.get("/get-started")
    assert r.status_code == 200
    assert "/auth/login?role=creator" in r.text
    assert "/auth/login?role=brand" in r.text
    # Operator card removed from the visual chooser — operators reach
    # /auth/login?role=operator via direct URL from an admin invite.
    # The route still accepts ?role=operator and the auth gate still
    # works; see test_get_started_with_role_query_redirects below.
    assert "/auth/login?role=operator" not in r.text
    assert "i manage babyg" not in r.text.lower()


@pytest.mark.parametrize("role", ["creator", "brand", "operator"])
def test_get_started_with_role_query_redirects(
    client: TestClient, role: str
) -> None:
    r = client.get(f"/get-started?role={role}")
    assert r.status_code == 302
    assert r.headers["location"] == f"/auth/login?role={role}"


def test_get_started_with_invalid_role_renders_cards(client: TestClient) -> None:
    # Invalid role shouldn't redirect — fall through to the chooser.
    r = client.get("/get-started?role=ufo")
    assert r.status_code == 200
    assert "/auth/login?role=creator" in r.text


def test_robots_blocks_indexing(client: TestClient) -> None:
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "User-agent: *" in r.text
    assert "Disallow: /" in r.text
