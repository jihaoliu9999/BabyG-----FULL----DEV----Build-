"""Onboarding wizard tests.

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
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_module
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# In-memory profiles fake
# -----------------------------------------------------------------------------


class FakeProfileStore:
    def __init__(self) -> None:
        self.creator: dict[str, dict[str, Any]] = {}
        self.brand: dict[str, dict[str, Any]] = {}
        self.last_creator_payload: dict[str, Any] | None = None
        self.last_brand_payload: dict[str, Any] | None = None
        self.complete_creator_should_fail = False
        self.complete_brand_should_fail = False


@pytest.fixture()
def store(monkeypatch) -> FakeProfileStore:
    s = FakeProfileStore()

    def _get_creator(uid: str):
        return s.creator.get(uid)

    def _is_creator(uid: str) -> bool:
        p = s.creator.get(uid)
        return bool(p and p.get("onboarding_completed_at"))

    def _get_brand(uid: str):
        return s.brand.get(uid)

    def _is_brand(uid: str) -> bool:
        p = s.brand.get(uid)
        return bool(p and p.get("onboarding_completed_at"))

    def _complete_creator(uid: str, payload: dict[str, Any]) -> bool:
        if s.complete_creator_should_fail:
            return False
        # Mirror the real implementation: add the timestamp.
        full = {**payload, "onboarding_completed_at": "2026-05-07T00:00:00Z"}
        s.last_creator_payload = full
        s.creator.setdefault(uid, {}).update(full)
        return True

    def _complete_brand(uid: str, payload: dict[str, Any]) -> bool:
        if s.complete_brand_should_fail:
            return False
        full = {**payload, "onboarding_completed_at": "2026-05-07T00:00:00Z"}
        s.last_brand_payload = full
        s.brand.setdefault(uid, {}).update(full)
        return True

    monkeypatch.setattr(profiles_module, "get_creator_profile", _get_creator)
    monkeypatch.setattr(profiles_module, "is_creator_onboarded", _is_creator)
    monkeypatch.setattr(profiles_module, "complete_creator_onboarding", _complete_creator)
    monkeypatch.setattr(profiles_module, "get_brand_profile", _get_brand)
    monkeypatch.setattr(profiles_module, "is_brand_onboarded", _is_brand)
    monkeypatch.setattr(profiles_module, "complete_brand_onboarding", _complete_brand)
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
    assert "tell us the basics." in r.text
    assert "you're almost in." not in r.text
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
    assert 'name="instagram_handle"' not in r.text
    assert "@annareyes" in r.text
    # Selected chips for fashion + lifestyle should be checked. The macro
    # renders `<input ... value="fashion" checked />` (whitespace-collapsed).
    text_squashed = " ".join(r.text.split())
    assert 'value="fashion" checked' in text_squashed
    assert 'value="lifestyle" checked' in text_squashed


def test_brand_wizard_renders_blank_for_new_user(client, store):
    _signed_in(client, role="brand")
    r = client.get("/onboarding/brand")
    assert r.status_code == 200
    assert "brand setup" in r.text
    assert "who is the brand?" in r.text
    assert 'name="company_name"' in r.text


def test_brand_wizard_redirects_when_already_onboarded(client, store):
    _signed_in(client, role="brand")
    store.brand["u-1"] = {"onboarding_completed_at": "2026-05-07T00:00:00Z"}
    r = client.get("/onboarding/brand")
    assert r.status_code == 302
    assert r.headers["location"] == "/brand"


# -----------------------------------------------------------------------------
# POST /onboarding/creator
# -----------------------------------------------------------------------------


def _valid_creator_form() -> dict:
    return {
        "full_name": "Anna Reyes",
        "location_city": "Los Angeles",
        "location_region": "California",
        "bio": "food + fashion.",
        "niches": ["food", "fashion"],
        "content_formats": ["reels", "stories"],
        "primary_platform": "Instagram",
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
    assert p["instagram_handle"] is None
    assert p["location_city"] == "Los Angeles"
    assert p["location_region"] == "California"
    assert p["location_source"] == "manual"
    assert p["location_lat"] is None
    assert p["location_lng"] is None
    assert p["niches"] == ["food", "fashion"]
    assert p["content_formats"] == ["reels", "stories"]
    assert p["primary_platform"] == "Instagram"
    assert p["follower_range"] is None
    assert p["engagement_range"] is None
    assert p["creator_tenure"] is None
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
    form["content_formats"] = ["reels", "static", "carousels", "ugc"]
    form["primary_platform"] = "YouTube"

    r = client.post("/onboarding/creator", data=form)
    assert r.status_code == 303
    p = store.last_creator_payload
    assert "<script>" not in p["niches"]
    assert p["follower_range"] is None
    assert p["content_formats"] == ["reels"]
    assert p["primary_platform"] is None


def test_creator_submit_uses_instagram_connection_handle(
    monkeypatch, client, store
):
    from app.routes import onboarding as onboarding_routes

    _signed_in(client, role="creator", user_id="u-1")
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: {"metadata": {"username": "MiaCreates"}},
    )

    r = client.post("/onboarding/creator", data=_valid_creator_form())

    assert r.status_code == 303
    assert store.last_creator_payload["instagram_handle"] == "miacreates"


def test_creator_submit_saves_custom_other_niche(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    form["niches"] = ["food", "__other__"]
    form["niche_other"] = "Car Culture"

    r = client.post("/onboarding/creator", data=form)

    assert r.status_code == 303
    assert store.last_creator_payload["niches"] == ["food", "car culture"]


def test_creator_submit_rejects_invalid_browser_location(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    form["location_source"] = "browser"
    form["location_lat"] = "91"
    form["location_lng"] = "-118.2437"

    r = client.post("/onboarding/creator", data=form)
    assert r.status_code == 400
    assert "latitude" in r.text.lower()
    assert store.last_creator_payload is None


def test_creator_submit_accepts_valid_browser_location(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    form["location_source"] = "browser"
    form["location_lat"] = "34.0522"
    form["location_lng"] = "-118.2437"

    r = client.post("/onboarding/creator", data=form)

    assert r.status_code == 303
    p = store.last_creator_payload
    assert p["location_source"] == "browser"
    assert p["location_lat"] == 34.0522
    assert p["location_lng"] == -118.2437
    assert p["location_city"] == "Los Angeles"


def test_creator_submit_allows_missing_location(client, store):
    _signed_in(client, role="creator")
    form = _valid_creator_form()
    form.pop("location_city")
    form.pop("location_region")

    r = client.post("/onboarding/creator", data=form)

    assert r.status_code == 303
    p = store.last_creator_payload
    assert p["location_source"] is None
    assert p["location_city"] is None


# -----------------------------------------------------------------------------
# POST /onboarding/brand
# -----------------------------------------------------------------------------


def _valid_brand_form() -> dict:
    return {
        "company_name": "Studio House",
        "brand_website": "https://studio.example",
        "contact_full_name": "Alex Morgan",
        "contact_title": "partnerships",
        "product_description": "premium essentials.",
        "industry": "fashion",
        "scale_descriptor": "growth-stage",
        "model_descriptor": "DTC",
        "positioning_descriptor": "premium",
        "campaign_types": ["paid posts", "events"],
        "creator_size_preferences": ["micro", "mid"],
        "niche_preferences": ["fashion", "lifestyle"],
        "budget_range": "$5-15k",
    }


def test_brand_submit_saves_and_redirects(client, store):
    _signed_in(client, role="brand")

    r = client.post("/onboarding/brand", data=_valid_brand_form())

    assert r.status_code == 303
    assert r.headers["location"] == "/brand"
    assert store.last_brand_payload is not None
    p = store.last_brand_payload
    assert p["company_name"] == "Studio House"
    assert p["brand_website"] == "https://studio.example"
    assert p["contact_full_name"] == "Alex Morgan"
    assert p["campaign_types"] == ["paid posts", "events"]
    assert p["creator_size_preferences"] == ["micro", "mid"]
    assert "onboarding_completed_at" in p


def test_brand_submit_rejects_missing_company(client, store):
    _signed_in(client, role="brand")
    form = _valid_brand_form()
    del form["company_name"]
    r = client.post("/onboarding/brand", data=form)
    assert r.status_code == 400
    assert "company name" in r.text.lower()
    assert store.last_brand_payload is None


def test_brand_submit_rejects_unsafe_website(client, store):
    _signed_in(client, role="brand")
    form = _valid_brand_form()
    form["brand_website"] = "javascript:alert(1)"
    r = client.post("/onboarding/brand", data=form)
    assert r.status_code == 400
    assert "valid http" in r.text.lower()
    assert store.last_brand_payload is None


def test_brand_submit_drops_unknown_enum_values(client, store):
    _signed_in(client, role="brand")
    form = _valid_brand_form()
    form["campaign_types"] = ["paid posts", "<script>"]
    form["creator_size_preferences"] = ["macro", "giant"]
    form["industry"] = "evil"

    r = client.post("/onboarding/brand", data=form)

    assert r.status_code == 303
    p = store.last_brand_payload
    assert p["campaign_types"] == ["paid posts"]
    assert p["creator_size_preferences"] == ["macro"]
    assert p["industry"] is None


# -----------------------------------------------------------------------------
# Dashboard onboarding gate
# -----------------------------------------------------------------------------


def test_creator_dashboard_redirects_to_onboarding_when_incomplete(client, store):
    _signed_in(client, role="creator")
    # No profile row yet → not onboarded → redirect.
    r = client.get("/creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/creator"


def test_brand_dashboard_redirects_to_onboarding_when_incomplete(client, store):
    _signed_in(client, role="brand")
    r = client.get("/brand")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/brand"


def test_brand_dashboard_renders_when_onboarded(client, store):
    """Onboarded brands now land on a real dashboard page instead of
    the legacy redirect to /brand/discover. The page surfaces profile
    completion + activity counts; if any of the count-services blow
    up the route degrades to zero — see _dashboard_counts in brand.py."""
    _signed_in(client, role="brand")
    store.brand["u-1"] = {
        "company_name": "Studio House",
        "brand_website": "https://studio.example",
        "contact_full_name": "Alex Morgan",
        "campaign_types": ["paid posts"],
        "creator_size_preferences": ["micro"],
        "onboarding_completed_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/brand")
    assert r.status_code == 200
    assert "Studio House" in r.text
    assert "brand dashboard" in r.text.lower()
    # Quick actions link to every brand surface so a brand can navigate
    # from the dashboard alone.
    assert "/brand/campaigns/new" in r.text
    assert "/brand/discover" in r.text
    assert "/brand/profile" in r.text
    assert "/brand/dm" in r.text


def test_brand_discover_renders_when_onboarded(client, store, monkeypatch):
    _signed_in(client, role="brand")
    store.brand["u-1"] = {
        "company_name": "Studio House",
        "brand_website": "https://studio.example",
        "contact_full_name": "Alex Morgan",
        "campaign_types": ["paid posts"],
        "creator_size_preferences": ["micro"],
        "niche_preferences": ["fashion"],
        "onboarding_completed_at": "2026-05-07T00:00:00Z",
    }
    creator_id = str(uuid4())

    monkeypatch.setattr(
        discover_module,
        "list_cards",
        lambda **kwargs: [
            {
                "card_kind": "creator",
                "card_id": creator_id,
                "owner_user_id": creator_id,
                "title": "Mia Santos",
                "subtitle": "@mia",
                "image_url": None,
                "location_label": "Miami, FL",
                "tags": ["fashion"],
                "description": "fashion creator",
                "primary_platform": "Instagram",
                "follower_range": "10k-50k",
                "compensation_text": None,
                "deadline": None,
                "detail_path": f"/brand/discover/creator/{creator_id}",
                "why_relevant": "matches your fashion focus",
            }
        ],
    )
    monkeypatch.setattr(discover_module, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(discover_module, "record_action", lambda **kwargs: True)

    r = client.get("/brand/discover")
    assert r.status_code == 200
    assert "Mia Santos" in r.text
    assert "brand path is active" not in r.text


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


def test_onboarding_brand_requires_brand_role(client, store):
    _signed_in(client, role="creator")
    r = client.get("/onboarding/brand")
    assert r.status_code == 403


def test_onboarding_brand_requires_auth(client, store):
    r = client.get("/onboarding/brand")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?role=brand"


# ---------------------------------------------------------------------------
# Stepped wizard structure: the form is organized into three focused
# <fieldset data-onb-step="N"> sections. Pin the markers so a future
# refactor that drops one of the steps surfaces clearly.
# ---------------------------------------------------------------------------


def test_onboarding_creator_renders_three_steps_and_integrations(
    monkeypatch, client, store
):
    from app.routes import onboarding as onboarding_routes

    _signed_in(client, role="creator", user_id="u-1")

    monkeypatch.setattr(
        onboarding_routes.google_calendar,
        "is_configured",
        lambda: False,
    )
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        onboarding_routes.instagram_meta,
        "is_configured",
        lambda: False,
    )
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: None,
    )

    r = client.get("/onboarding/creator")
    assert r.status_code == 200
    # Three step pips.
    for n in range(1, 4):
        assert f'data-onb-step-pip="{n}"' in r.text
    assert 'data-onb-step-pip="4"' not in r.text
    # Three step fieldsets.
    for n in range(1, 4):
        assert f'data-onb-step="{n}"' in r.text
    assert 'data-onb-step="4"' not in r.text
    assert "welcome" not in r.text.lower()
    assert "you're almost in." not in r.text
    assert "basics</span>" not in r.text
    # Integrations cards include the provider labels and simple status
    # states, without collecting manual social stat questions.
    assert "let babyg use my location" in r.text
    assert 'name="location_city"' in r.text
    assert 'name="neighborhood"' not in r.text
    assert "calendar" in r.text
    assert "gmail" in r.text
    assert "instagram" in r.text
    assert "tiktok" in r.text
    assert "coming soon" in r.text
    assert "/creator/instagram/connect" not in r.text
    assert "/creator/tiktok/connect" not in r.text
    assert "follower range" not in r.text
    assert "engagement rate" not in r.text
    assert "creator tenure" not in r.text
    assert 'name="instagram_handle"' not in r.text
    assert 'value="static"' not in r.text
    assert 'value="ugc"' not in r.text
    assert 'value="carousels"' not in r.text
    assert 'value="YouTube"' not in r.text


def test_onboarding_passes_google_flags_when_configured(monkeypatch, client, store):
    from app.routes import onboarding as onboarding_routes

    _signed_in(client, role="creator", user_id="u-1")
    monkeypatch.setattr(
        onboarding_routes.google_calendar, "is_configured", lambda: True
    )
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: None,
    )

    r = client.get("/onboarding/creator")
    assert r.status_code == 200
    # Real connect link visible for Google Calendar.
    assert (
        "/creator/google/connect?service=calendar&amp;next=/onboarding/creator"
        in r.text
    )
    assert (
        "/creator/google/connect?service=gmail&amp;next=/onboarding/creator"
        in r.text
    )
    assert "calendar" in r.text
    assert "gmail" in r.text


def test_onboarding_prefills_handle_from_instagram_metadata(
    monkeypatch, client, store
):
    from app.routes import onboarding as onboarding_routes

    _signed_in(client, role="creator", user_id="u-1")
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        onboarding_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: {"metadata": {"username": "miacreates"}},
    )
    monkeypatch.setattr(
        onboarding_routes.instagram_meta,
        "is_configured",
        lambda: True,
    )

    r = client.get("/onboarding/creator")

    assert r.status_code == 200
    assert "@miacreates" in r.text
    assert 'name="instagram_handle"' not in r.text
