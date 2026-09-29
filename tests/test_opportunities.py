"""Post-opportunity form + submit endpoint at /creator/opportunities/new.

Locks in the three things the ship depends on:
  * form GET renders 200 with all four kind choices
  * happy-path POST calls jobs.create with a clean payload and
    redirects to Discover's opportunity tab
  * validation POSTs re-render 400 with the error banner and don't
    call jobs.create
"""

from __future__ import annotations

from pathlib import Path

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
    assert '<h1 class="op-new-title">' not in r.text
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


@pytest.mark.parametrize("listing_type", ["ugc_gig", "collab", "hiring", "brand_deal"])
@pytest.mark.parametrize("compensation_type", ["gifted", "negotiable"])
def test_structured_opportunity_submission(
    client, monkeypatch, listing_type, compensation_type
):
    _signed_in(client)
    _stub_profile(monkeypatch)
    captured = []
    monkeypatch.setattr(jobs_module, "create", lambda **kw: captured.append(kw) or "new-id")
    csrf = _get_csrf(client)
    response = client.post("/creator/opportunities/new", data={
        "csrf_token": csrf, "title": "Real scope", "description": "x" * 2000,
        "listing_type": listing_type, "compensation_type": compensation_type,
        "compensation_text": "$999 injected", "location": " Remote, U.S. Only ",
        "deadline": "2026-10-15", "target_niches": "Food, wellness, , ugc",
    })
    assert response.status_code == 303
    assert response.headers["location"] == "/creator/discover?kind=opportunity&posted=1"
    assert captured[0]["poster_id"] == "creator-1"
    assert captured[0]["payload"] == {
        "title": "Real scope", "description": "x" * 2000, "listing_type": listing_type,
        "compensation_type": compensation_type,
        "compensation_text": opp_routes.COMPENSATION_LABELS[compensation_type],
        "location_city": "Remote, U.S. Only", "deadline": "2026-10-15T23:59:59+00:00",
        "target_niches": ["food", "wellness", "ugc"],
    }


def test_location_is_optional_and_description_limit_is_preserved(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    captured = []
    monkeypatch.setattr(jobs_module, "create", lambda **kw: captured.append(kw) or "new-id")
    csrf = _get_csrf(client)
    response = client.post("/creator/opportunities/new", data={
        "csrf_token": csrf, "title": "Scope", "description": "x" * 2001,
        "listing_type": "collab", "compensation_type": "negotiable",
    })
    assert response.status_code == 303
    assert "location_city" not in captured[0]["payload"]
    assert len(captured[0]["payload"]["description"]) == 2000


@pytest.mark.parametrize("compensation_type", ["paid", "cash", "flat_rate", "unknown"])
def test_new_compensation_rejects_unsupported_choices(client, monkeypatch, compensation_type):
    _signed_in(client)
    _stub_profile(monkeypatch)
    calls = []
    monkeypatch.setattr(jobs_module, "create", lambda **kw: calls.append(kw))
    csrf = _get_csrf(client)
    response = client.post("/creator/opportunities/new", data={
        "csrf_token": csrf, "title": "Scope", "description": "Scope",
        "listing_type": "collab", "compensation_type": compensation_type,
    })
    assert response.status_code == 400
    assert calls == []


def test_form_identity_preview_and_locked_controls(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    calls = []
    monkeypatch.setattr(jobs_module, "create", lambda **kw: calls.append(kw))
    response = client.get("/creator/opportunities/new")
    html = response.text
    assert response.status_code == 200
    assert calls == []
    assert '<legend>opportunity type</legend>' in html
    assert '<legend>compensation</legend>' in html
    assert 'name="compensation_type" value="gifted"' in html
    assert 'name="compensation_type" value="negotiable"' in html
    assert 'name="compensation_text"' not in html
    assert 'name="budget_min"' not in html and 'name="budget_max"' not in html
    assert 'name="location" maxlength="120"' in html
    assert 'maxlength="2000"' in html
    assert "agree to direct wire" not in html
    assert "op-new-wire" not in html
    assert '<strong>Anna</strong>' in html
    assert '<h1' not in html
    assert '<h2>preview' not in html
    assert 'href="/creator/discover?kind=opportunity"' in html
    assert 'action="/creator/opportunities/new"' in html
    assert html.index('class="op-new-submit"') < html.index('class="op-new-preview"')
    for href in ["/creator", "/creator/discover", "/creator/dm", "/creator/profile/settings"]:
        assert f'href="{href}"' in html


def test_failed_save_preserves_structured_form_values(client, monkeypatch):
    _signed_in(client)
    _stub_profile(monkeypatch)
    monkeypatch.setattr(jobs_module, "create", lambda **kw: None)
    csrf = _get_csrf(client)
    response = client.post("/creator/opportunities/new", data={
        "csrf_token": csrf, "title": "Scope", "description": "Details",
        "listing_type": "hiring", "compensation_type": "negotiable",
        "location": "Miami", "target_niches": "food", "deadline": "2026-10-15",
    })
    assert response.status_code == 200
    assert 'value="negotiable" checked' in response.text
    assert 'value="hiring" checked' in response.text
    assert 'value="Miami"' in response.text
    assert 'value="2026-10-15"' in response.text


def test_preview_code_has_no_write_or_payment_path():
    root = Path(__file__).resolve().parents[1]
    js = (root / "app/static/js/opportunity_new.js").read_text()
    for forbidden in ["fetch(", "XMLHttpRequest", "requestSubmit(", ".submit(", "innerHTML", "localStorage", "stripe"]:
        assert forbidden not in js
    assert 'form.addEventListener("input", update)' in js
    assert 'form.addEventListener("change", update)' in js
    assert "textContent" in js


def test_opportunity_layout_is_scoped_and_mobile_first():
    root = Path(__file__).resolve().parents[1]
    css = (root / "app/static/css/app.css").read_text()
    opportunity_css = css.split(".op-new {", 1)[1].split(
        "/* Calendar detail page", 1
    )[0]
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in opportunity_css
    assert "@media (max-width: 359px)" in opportunity_css
    assert ".op-new-comp-row { grid-template-columns: minmax(0, 1fr); }" in opportunity_css
    assert "@media (min-width: 1000px)" in opportunity_css
    assert "width: min(100%, 1080px)" in opportunity_css
    assert "width: 44px; height: 44px" in opportunity_css
    assert "width: 100%; min-width: 0; max-width: 100%" in opportunity_css
    assert "overflow-wrap: anywhere" in opportunity_css
    assert "input:focus-visible" in opportunity_css
    assert ".app-tabbar" not in opportunity_css
    assert "--tabbar-h" not in opportunity_css
