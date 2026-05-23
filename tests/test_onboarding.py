"""Onboarding wizard tests.

v1 ships creator-only onboarding. Brand onboarding tests shipped on the
brand-side-v1.5 branch.

Stubs the profiles service so DB calls become in-memory dict ops. Covers:

  * GET wizard renders pre-filled fields
  * POST creator: success path saves payload + onboarding timestamp + redirect
  * POST creator: validation rejects missing required fields
  * POST creator: invalid enum values are dropped, not echoed back
  * Already-onboarded users are bounced from the wizard back to dashboard
  * /creator dashboard redirects to /onboarding/creator when not yet onboarded
  * /onboarding/operator renders without a profile lookup
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# In-memory profiles fake
# -----------------------------------------------------------------------------


class FakeProfileStore:
    def __init__(self) -> None:
        self.creator: dict[str, dict[str, Any]] = {}
        self.last_creator_payload: dict[str, Any] | None = None
        self.complete_creator_should_fail = False


@pytest.fixture()
def store(monkeypatch) -> FakeProfileStore:
    s = FakeProfileStore()

    def _get_creator(uid: str):
        return s.creator.get(uid)

    def _is_creator(uid: str) -> bool:
        p = s.creator.get(uid)
        return bool(p and p.get("onboarding_completed_at"))

    def _complete_creator(uid: str, payload: dict[str, Any]) -> bool:
        if s.complete_creator_should_fail:
            return False
        # Mirror the real implementation: add the timestamp.
        full = {**payload, "onboarding_completed_at": "2026-05-07T00:00:00Z"}
        s.last_creator_payload = full
        s.creator.setdefault(uid, {}).update(full)
        return True

    monkeypatch.setattr(profiles_module, "get_creator_profile", _get_creator)
    monkeypatch.setattr(profiles_module, "is_creator_onboarded", _is_creator)
    monkeypatch.setattr(profiles_module, "complete_creator_onboarding", _complete_creator)
    return s


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str, user_id: str = "u-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


# -----------------------------------------------------------------------------
# GET wizard
# -----------------------------------------------------------------------------


def test_creator_wizard_renders_blank_for_new_user(client, store):
    _signed_in(client, role="creator")
    r = client.get("/onboarding/creator")
    assert r.status_code == 200
    assert "tell us about your work" in r.text
    # All chip groups should be present.
    for niche in ["food", "fashion", "fitness"]:
        assert f'value="{niche}"' in r.text


def test_operator_welcome_renders(client, store):
    _signed_in(client, role="operator")
    r = client.get("/onboarding/operator")
    assert r.status_code == 200
    assert "Welcome to the console" in r.text


def test_creator_wizard_redirects_when_already_onboarded(client, store):
    _signed_in(client, role="creator")
    store.creator["u-1"] = {"onboarding_completed_at": "2026-05-07T00:00:00Z"}
    r = client.get("/onboarding/creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/creator"


def test_creator_wizard_pre_fills_existing_values(client, store):
    _signed_in(client, role="creator")
    store.creator["u-1"] = {
        "full_name": "Anna Reyes",
        "instagram_handle": "annareyes",
        "niches": ["fashion", "lifestyle"],
        "primary_platform": "Instagram",
    }
    r = client.get("/onboarding/creator")
    assert r.status_code == 200
    assert 'value="Anna Reyes"' in r.text
    assert 'value="annareyes"' in r.text
    # Selected chips for fashion + lifestyle should be checked. The macro
    # renders `<input ... value="fashion" checked />` (whitespace-collapsed).
    text_squashed = " ".join(r.text.split())
    assert 'value="fashion" checked' in text_squashed
    assert 'value="lifestyle" checked' in text_squashed


# -----------------------------------------------------------------------------
# POST /onboarding/creator
# -----------------------------------------------------------------------------


def _valid_creator_form() -> dict:
    return {
        "full_name": "Anna Reyes",
        "instagram_handle": "@AnnaReyes",
        "neighborhood": "Wynwood",
        "bio": "Miami food + fashion.",
        "niches": ["food", "fashion"],
        "content_formats": ["reels", "carousels"],
        "primary_platform": "Instagram",
        "follower_range": "10-50k",
        "engagement_range": "4-7%",
        "creator_tenure": "1-2y",
        "hard_limits": ["no fast fashion"],
        "tier": "pro",
    }


def test_creator_submit_saves_and_redirects(client, store):
    _signed_in(client, role="creator")

    r = client.post("/onboarding/creator", data=_valid_creator_form())

    assert r.status_code == 303
    assert r.headers["location"] == "/creator"
    assert store.last_creator_payload is not None
    p = store.last_creator_payload
    assert p["full_name"] == "Anna Reyes"
    assert p["instagram_handle"] == "annareyes"      # @ stripped, lowercased
    assert p["neighborhood"] == "Wynwood"
    assert p["niches"] == ["food", "fashion"]
    assert p["content_formats"] == ["reels", "carousels"]
    assert p["primary_platform"] == "Instagram"
    assert p["follower_range"] == "10-50k"
    assert p["tier"] == "pro"
    assert "onboarding_completed_at" in p


def test_creator_submit_rejects_missing_full_name(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    del form["full_name"]
    r = client.post("/onboarding/creator", data=form)
    assert r.status_code == 400
    assert "full name" in r.text.lower()
    assert store.last_creator_payload is None


def test_creator_submit_rejects_no_niches(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    del form["niches"]
    r = client.post("/onboarding/creator", data=form)
    assert r.status_code == 400
    assert "niche" in r.text.lower()


def test_creator_submit_drops_unknown_enum_values(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    form["niches"] = [*form["niches"], "<script>"]
    form["follower_range"] = "9999G"

    r = client.post("/onboarding/creator", data=form)
    assert r.status_code == 303
    p = store.last_creator_payload
    assert "<script>" not in p["niches"]
    assert p["follower_range"] is None


def test_creator_submit_rejects_invalid_neighborhood(client, store):
    # An invalid neighborhood used to silently clear the field while
    # reporting success. It must surface as a form error instead.
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    form["neighborhood"] = "Atlantis"

    r = client.post("/onboarding/creator", data=form)
    assert r.status_code == 400
    assert "neighborhood" in r.text.lower()
    assert store.last_creator_payload is None


# -----------------------------------------------------------------------------
# Dashboard onboarding gate
# -----------------------------------------------------------------------------


def test_creator_dashboard_redirects_to_onboarding_when_incomplete(client, store):
    _signed_in(client, role="creator")
    # No profile row yet → not onboarded → redirect.
    r = client.get("/creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/creator"


# `creator dashboard renders when onboarded` and `operator dashboard skips
# the onboarding check` are now covered by tests/test_intel.py, which stubs
# the intel service. The dashboards do real work post-Phase-1-Step-5 and
# can't be tested with only the profiles store stubbed.


# -----------------------------------------------------------------------------
# Authorization
# -----------------------------------------------------------------------------


def test_onboarding_creator_requires_creator_role(client, store):
    _signed_in(client, role="operator")
    r = client.get("/onboarding/creator")
    assert r.status_code == 403


def test_onboarding_creator_requires_auth(client, store):
    # HTML GET → redirect to login; JSON GET → 401.
    r = client.get("/onboarding/creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?role=creator"
    r = client.get("/onboarding/creator", headers={"accept": "application/json"})
    assert r.status_code == 401
