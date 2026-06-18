"""Render + behavior tests for the brand-side UI pass.

Covers the surfaces added in the brand-frontend polish pass: dashboard,
profile (identity + preferences), campaigns list, campaigns new, saved,
dm placeholder, and the brand tabbar partial. All service calls are
stubbed so tests never hit Supabase.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import brand as brand_routes
from app.services import dms as dms_module
from app.services import jobs as jobs_module
from app.services import network as network_module
from app.services import profiles as profiles_module


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str, user_id: str = "brand-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _brand_profile(**overrides: Any) -> dict[str, Any]:
    base = {
        "user_id": "brand-1",
        "company_name": "Studio House",
        "brand_website": "https://studio.example",
        "industry": "fashion",
        "contact_full_name": "Alex Morgan",
        "contact_title": "head of partnerships",
        "campaign_types": ["paid_post", "ugc"],
        "creator_size_preferences": ["micro", "mid"],
        "niche_preferences": ["fashion", "lifestyle"],
        "budget_range": "5k_25k",
        "product_description": "boutique fragrance line based in LA.",
        "is_verified": False,
        "onboarding_completed_at": "2026-05-07T00:00:00Z",
    }
    base.update(overrides)
    return base


@pytest.fixture()
def stub_brand(monkeypatch):
    """Stub every external service the brand routes call. Defaults are
    minimal/empty so render tests focus on the page, not the data shape."""
    saved: dict[str, Any] = {
        "brand": _brand_profile(),
        "listings": [],
        "updates": [],
        "created_listings": [],
        "saved_creators": [],
        "saved_action_rows": [],
    }

    monkeypatch.setattr(
        profiles_module, "get_brand_profile", lambda uid: dict(saved["brand"])
    )

    def _update_brand_profile(uid: str, payload: dict[str, Any]) -> bool:
        saved["updates"].append(payload)
        saved["brand"].update(payload)
        return True

    monkeypatch.setattr(profiles_module, "update_brand_profile", _update_brand_profile)
    monkeypatch.setattr(
        profiles_module,
        "get_creators_by_ids",
        lambda ids: {c["user_id"]: c for c in saved["saved_creators"] if c["user_id"] in ids},
    )
    monkeypatch.setattr(
        jobs_module, "list_by_poster", lambda uid, **kw: list(saved["listings"])
    )

    def _create(*, poster_id: str, payload: dict[str, Any]) -> str | None:
        if saved.get("create_fails"):
            return None
        new_id = f"listing-{len(saved['created_listings'])+1}"
        saved["created_listings"].append({"poster_id": poster_id, "payload": payload, "id": new_id})
        return new_id

    monkeypatch.setattr(jobs_module, "create", _create)
    monkeypatch.setattr(network_module, "list_incoming_pending", lambda uid: [])
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(dms_module, "list_threads_for_user", lambda uid: [])

    # _list_saved_creators uses a raw supabase query; intercept the whole
    # helper so we don't have to stub PostgREST chains.
    monkeypatch.setattr(
        brand_routes,
        "_list_saved_creators",
        lambda brand_user_id: list(saved["saved_creators"]),
    )
    return saved


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_dashboard_renders_with_completion_meter_and_quick_actions(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.get("/brand")
    assert r.status_code == 200
    assert "brand dashboard" in r.text.lower()
    assert "Studio House" in r.text
    # Completion meter renders as a percentage int.
    assert "%</strong>" in r.text
    # Every quick-action links to a real surface.
    for path in [
        "/brand/campaigns/new",
        "/brand/discover",
        "/brand/profile",
        "/brand/dm",
    ]:
        assert path in r.text
    # Activity tiles render real counts (zero when stubbed empty).
    assert "active campaigns" in r.text.lower()
    assert "saved creators" in r.text.lower()
    assert "unread messages" in r.text.lower()
    assert "inbound interest" in r.text.lower()


def test_dashboard_shows_missing_fields_when_incomplete(
    client: TestClient, stub_brand
) -> None:
    """If the brand hasn't filled the optional fields (logo, description),
    the completion meter should list them as missing — no fake 100%."""
    _signed_in(client, role="brand")
    stub_brand["brand"] = _brand_profile(
        logo_url=None,
        product_description="",
        niche_preferences=[],
    )
    r = client.get("/brand")
    assert r.status_code == 200
    assert "still missing" in r.text.lower()


def test_dashboard_redirects_to_onboarding_when_incomplete(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    stub_brand["brand"] = {**_brand_profile(), "onboarding_completed_at": None}
    r = client.get("/brand")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/brand"


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


def test_profile_page_renders_with_preview_card_and_forms(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.get("/brand/profile")
    assert r.status_code == 200
    assert "discover preview" in r.text.lower()
    # Card preview surfaces the public-projection fields.
    assert "Studio House" in r.text
    assert "https://studio.example" in r.text
    # Both edit forms render with the right actions.
    assert 'action="/brand/profile/identity"' in r.text
    assert 'action="/brand/profile/preferences"' in r.text
    # Closed-vocab selects pre-select the saved value.
    assert '<option value="fashion" selected' in r.text
    assert '<option value="5k_25k" selected' in r.text


def test_profile_identity_update_persists_normalized_payload(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.post(
        "/brand/profile/identity",
        data={
            "company_name": "  Studio   House  ",
            "brand_website": "https://studio.example",
            "industry": "beauty",
            "contact_full_name": "Alex Morgan",
            "contact_title": "Head of Partnerships",
            "product_description": "  fragrance brand  ",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/profile?identity=ok"
    payload = stub_brand["updates"][-1]
    assert payload["company_name"] == "Studio House"
    assert payload["industry"] == "beauty"
    assert payload["product_description"] == "fragrance brand"


def test_profile_identity_update_drops_unknown_industry(
    client: TestClient, stub_brand
) -> None:
    """Closed vocab silently drops unknown values rather than letting
    them reach the DB (where there's no CHECK constraint on industry
    today, but treating it as closed vocab keeps Discover ranking
    consistent)."""
    _signed_in(client, role="brand")
    r = client.post(
        "/brand/profile/identity",
        data={
            "company_name": "Studio House",
            "brand_website": "",
            "industry": "interplanetary",
            "contact_full_name": "Alex Morgan",
            "contact_title": "",
            "product_description": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    payload = stub_brand["updates"][-1]
    assert payload["industry"] is None
    # Empty website/title/description clear to NULL rather than persisting "".
    assert payload["brand_website"] is None
    assert payload["contact_title"] is None
    assert payload["product_description"] is None


def test_profile_preferences_update_persists_chip_lists(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.post(
        "/brand/profile/preferences",
        data={
            "campaign_types": ["ugc", "paid_post", "totally_invented"],
            "creator_size_preferences": ["nano", "nano"],
            "niche_preferences": ["fashion", "wellness"],
            "budget_range": "25k_plus",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/profile?preferences=ok"
    payload = stub_brand["updates"][-1]
    assert payload["campaign_types"] == ["ugc", "paid_post"]
    assert payload["creator_size_preferences"] == ["nano"]
    assert payload["niche_preferences"] == ["fashion", "wellness"]
    assert payload["budget_range"] == "25k_plus"


# ---------------------------------------------------------------------------
# Campaigns
# ---------------------------------------------------------------------------


def test_campaigns_list_renders_empty_state(client: TestClient, stub_brand) -> None:
    _signed_in(client, role="brand")
    r = client.get("/brand/campaigns")
    assert r.status_code == 200
    assert "no campaigns yet" in r.text.lower()
    assert "/brand/campaigns/new" in r.text


def test_campaigns_list_renders_listings(client: TestClient, stub_brand) -> None:
    _signed_in(client, role="brand")
    stub_brand["listings"] = [
        {
            "id": "lst-1",
            "title": "spring fragrance launch",
            "description": "looking for 3 reels from beauty creators.",
            "compensation_text": "$1500 + product",
            "target_niches": ["beauty", "fashion"],
            "is_active": True,
            "is_taken_down": False,
            "created_at": "2026-05-07T00:00:00Z",
        }
    ]
    r = client.get("/brand/campaigns")
    assert r.status_code == 200
    assert "spring fragrance launch" in r.text
    assert "$1500 + product" in r.text


def test_campaigns_new_form_renders_with_brand_niches_prefilled(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.get("/brand/campaigns/new")
    assert r.status_code == 200
    assert "post a brief" in r.text.lower()
    # Brand's profile niches (fashion, lifestyle) pre-check those chips.
    text = " ".join(r.text.split())
    assert 'value="fashion" checked' in text
    assert 'value="lifestyle" checked' in text


def test_campaigns_create_persists_listing_and_redirects(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.post(
        "/brand/campaigns",
        data={
            "title": "  spring fragrance   launch  ",
            "description": "3 reels, beauty creators, april delivery.",
            "compensation_text": "$1500 + product",
            "target_niches": ["beauty", "fashion"],
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/campaigns?created=ok"
    created = stub_brand["created_listings"][-1]
    assert created["poster_id"] == "brand-1"
    assert created["payload"]["title"] == "spring fragrance launch"
    assert created["payload"]["listing_type"] == "brand_deal"
    assert created["payload"]["target_niches"] == ["beauty", "fashion"]
    assert created["payload"]["compensation_text"] == "$1500 + product"


def test_campaigns_create_rejects_missing_title(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.post(
        "/brand/campaigns",
        data={"title": "   ", "description": "anything"},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "required" in r.text.lower()
    assert stub_brand["created_listings"] == []


# ---------------------------------------------------------------------------
# Saved + DM placeholders
# ---------------------------------------------------------------------------


def test_saved_renders_empty_state(client: TestClient, stub_brand) -> None:
    _signed_in(client, role="brand")
    r = client.get("/brand/saved")
    assert r.status_code == 200
    assert "nothing saved yet" in r.text.lower()


def test_saved_renders_saved_creators_when_present(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    stub_brand["saved_creators"] = [
        {
            "user_id": "creator-9",
            "full_name": "Maya Chen",
            "instagram_handle": "mayachen",
            "niches": ["fashion"],
            "follower_range": "50k-100k",
            "primary_platform": "instagram",
            "location_label": "Los Angeles, California",
        }
    ]
    r = client.get("/brand/saved")
    assert r.status_code == 200
    assert "Maya Chen" in r.text
    assert "/brand/discover/creator/creator-9" in r.text


def test_dm_renders_placeholder_with_real_thread_count_when_any(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    # No threads → placeholder still renders, no "you have N existing
    # thread" line shown.
    r = client.get("/brand/dm")
    assert r.status_code == 200
    assert "brand messaging is coming soon" in r.text.lower()
    assert "you have" not in r.text.lower()


# ---------------------------------------------------------------------------
# Role guards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/brand",
        "/brand/profile",
        "/brand/campaigns",
        "/brand/campaigns/new",
        "/brand/saved",
        "/brand/dm",
    ],
)
def test_creator_session_cannot_reach_brand_routes(
    client: TestClient, stub_brand, path: str
) -> None:
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get(path)
    assert r.status_code == 403


@pytest.mark.parametrize(
    "path",
    [
        "/brand",
        "/brand/profile",
        "/brand/campaigns",
        "/brand/campaigns/new",
        "/brand/saved",
        "/brand/dm",
    ],
)
def test_anon_html_request_redirects_to_brand_login(
    client: TestClient, path: str
) -> None:
    r = client.get(path)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/auth/login?role=")


# ---------------------------------------------------------------------------
# Tabbar
# ---------------------------------------------------------------------------


def test_brand_pages_render_brand_tabbar_with_five_destinations(
    client: TestClient, stub_brand
) -> None:
    _signed_in(client, role="brand")
    r = client.get("/brand")
    assert r.status_code == 200
    # Five tab destinations all in the page (the tabbar partial is
    # included in base.html for is_brand). Verifying via hrefs rather
    # than label strings so a future copy tweak doesn't break the test.
    for href in [
        "/brand/discover",
        "/brand/campaigns",
        "/brand/dm",
        "/brand/saved",
        "/brand/profile",
    ]:
        assert href in r.text


# ---------------------------------------------------------------------------
# Onboarding gate (every brand route bounces incomplete profiles)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/brand",
        "/brand/profile",
        "/brand/campaigns",
        "/brand/campaigns/new",
        "/brand/saved",
        "/brand/dm",
    ],
)
def test_incomplete_brand_is_bounced_to_onboarding(
    client: TestClient, stub_brand, path: str
) -> None:
    _signed_in(client, role="brand")
    stub_brand["brand"] = {**_brand_profile(), "onboarding_completed_at": None}
    r = client.get(path)
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/brand"
