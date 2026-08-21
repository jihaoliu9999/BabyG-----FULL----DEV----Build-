"""Post-opportunity form + submit endpoint at /creator/opportunities/new.

Locks in the three things the ship depends on:
  * form GET renders 200 with all four kind choices
  * happy-path POST calls jobs.create with a clean payload and
    redirects to Discover's opportunity tab
  * validation POSTs re-render 400 with the error banner and don't
    call jobs.create
"""

from __future__ import annotations

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import opportunities as opp_routes
from app.services import jobs as jobs_module
from app.services import profiles as profiles_module


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, user_id: str = "creator-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _stub_profile(monkeypatch):
    monkeypatch.setattr(
        profiles_module,
        "get_creator_profile",
        lambda uid: {
            "user_id": uid,
            "full_name": "Anna",
            "onboarding_completed_at": "2026-05-01T00:00:00Z",
        },
    )


def _get_csrf(client: TestClient) -> str:
    """Pull the CSRF token from the rendered form. The form embeds it via
    the csrf_token partial."""
    r = client.get("/creator/opportunities/new")
    assert r.status_code == 200
    import re

    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', r.text)
    assert m, "csrf token missing from form"
    return m.group(1)


# ---------------------------------------------------------------------------
# GET /creator/opportunities/new
# ---------------------------------------------------------------------------


def test_new_opportunity_page_renders_all_kind_choices(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    r = client.get("/creator/opportunities/new")
    assert r.status_code == 200
    for choice in opp_routes.KIND_CHOICES:
        assert choice["label"] in r.text
        assert f'value="{choice["value"]}"' in r.text
    # Title + composer submit visible
    assert "post an opportunity" in r.text.lower()
    assert "post it" in r.text


def test_new_opportunity_requires_creator_role(client, monkeypatch):
    _signed_in(client, user_id="op-1")
    # simulate an operator role
    resp = Response()
    write_session(resp, {"user_id": "op-1", "role": "operator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    r = client.get("/creator/opportunities/new")
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# POST /creator/opportunities/new
# ---------------------------------------------------------------------------


def test_new_opportunity_submit_happy_path(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    captured: dict = {}

    def _create(*, poster_id, payload):
        captured["poster_id"] = poster_id
        captured["payload"] = payload
        return "listing-1"

    monkeypatch.setattr(jobs_module, "create", _create)

    csrf = _get_csrf(client)
    r = client.post(
        "/creator/opportunities/new",
        data={
            "csrf_token": csrf,
            "title": "  UGC brief — greek yogurt reels  ",
            "description": "  short recipe reels, delivery in a week.  ",
            "listing_type": "ugc_gig",
            "compensation_text": "$600-$1200",
            "target_niches": "food, wellness,  , FITNESS",
            "deadline": "2026-09-30",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/discover?kind=opportunity&posted=1"

    assert captured["poster_id"] == "creator-1"
    payload = captured["payload"]
    # Trimmed + capped strings, kind preserved, empty niches dropped + lowercased.
    assert payload["title"] == "UGC brief — greek yogurt reels"
    assert payload["description"] == "short recipe reels, delivery in a week."
    assert payload["listing_type"] == "ugc_gig"
    assert payload["compensation_text"] == "$600-$1200"
    assert payload["target_niches"] == ["food", "wellness", "fitness"]
    # Deadline is normalised to end-of-day UTC ISO.
    assert payload["deadline"].startswith("2026-09-30T23:59:59")


def test_new_opportunity_submit_missing_title_400s(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        jobs_module,
        "create",
        lambda **kw: calls.append(kw) or "should-not-fire",
    )
    csrf = _get_csrf(client)
    r = client.post(
        "/creator/opportunities/new",
        data={
            "csrf_token": csrf,
            "title": "   ",
            "description": "hi",
            "listing_type": "ugc_gig",
        },
    )
    assert r.status_code == 400
    assert "title" in r.text.lower()
    assert calls == []  # storage layer never hit


def test_new_opportunity_submit_bad_kind_400s(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    calls: list = []
    monkeypatch.setattr(
        jobs_module,
        "create",
        lambda **kw: calls.append(kw) or "should-not-fire",
    )
    csrf = _get_csrf(client)
    r = client.post(
        "/creator/opportunities/new",
        data={
            "csrf_token": csrf,
            "title": "valid",
            "description": "valid",
            "listing_type": "not-a-real-kind",
        },
    )
    assert r.status_code == 400
    assert calls == []


def test_new_opportunity_submit_storage_failure_shows_banner(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    monkeypatch.setattr(jobs_module, "create", lambda **kw: None)  # storage down
    csrf = _get_csrf(client)
    r = client.post(
        "/creator/opportunities/new",
        data={
            "csrf_token": csrf,
            "title": "valid",
            "description": "valid",
            "listing_type": "ugc_gig",
        },
    )
    assert r.status_code == 200
    # Jinja escapes the apostrophe to &#39; so match on the HTML form.
    assert "couldn&#39;t save" in r.text.lower()
    assert "op-new-banner" in r.text
