"""Explore opportunity cards must link to the VIEWER'S OWN role's detail page.

Regression for a Step 5A bug: the shared ``opportunity_card`` macro
hard-coded ``/creator/jobs/<id>`` and ignored the card's ``detail_path``,
so a brand tapping an Explore opportunity landed on a creator-only route
and got a 403. The backend already supplies the right path — the DB view
gives ``/creator/jobs/<id>`` and the brand route rewrites it to
``/brand/discover/opportunity/<id>`` — the template just wasn't using it.

The fix is purely which URL the link points at. These tests also lock in
that the role guards were NOT loosened to make the wrong link work.

Every test follows the rendered link and checks the destination opens, so
a link that "looks right" but 403s/404s cannot slip through.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import job_applications, jobs, network
from app.services import profiles as profiles_service

LISTING_ID = str(uuid4())
POSTER_ID = str(uuid4())
TITLE = "Autumn campaign reels"


def _listing() -> dict[str, Any]:
    return {
        "id": LISTING_ID,
        "poster_user_id": POSTER_ID,
        "poster_role": "brand",
        "title": TITLE,
        "description": "Three short reels.",
        "listing_type": "brand_deal",
        "compensation_text": "$2k",
        "compensation_type": "flat_rate",
        "budget_min": None,
        "budget_max": None,
        "target_niches": ["fashion"],
        "deadline": None,
        "is_active": True,
        "is_taken_down": False,
        "discovery_eligible": True,
        "expires_at": None,
        "location_city": None,
        "location_region": None,
        "location_country": None,
        "created_at": "2026-01-01T00:00:00Z",
    }


def _view_card() -> dict[str, Any]:
    """An opportunity card exactly as the ``discovery_cards`` view returns
    it: ``detail_path`` is creator-shaped for EVERY viewer (the brand route
    is responsible for rewriting it)."""
    return {
        "card_kind": "opportunity",
        "card_id": LISTING_ID,
        "owner_user_id": POSTER_ID,
        "title": TITLE,
        "subtitle": "Olipop",
        "image_url": None,
        "location_label": None,
        "tags": ["fashion"],
        "description": "Three short reels.",
        "compensation_text": "$2k",
        "budget_min": None,
        "budget_max": None,
        "deadline": None,
        "listing_type": "brand_deal",
        "detail_path": f"/creator/jobs/{LISTING_ID}",
        "relevance_reasons": [],
        "verification_status": None,
    }


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"cards": [_view_card()], "applied": set(), "owned": []}

    ok_creator = {"onboarding_completed_at": "2026-01-01T00:00:00Z", "niches": []}
    ok_brand = {
        "onboarding_completed_at": "2026-01-01T00:00:00Z",
        "company_name": "Olipop",
        "niche_preferences": [],
    }
    monkeypatch.setattr(profiles_service, "get_creator_profile", lambda uid: ok_creator)
    monkeypatch.setattr(
        profiles_service, "get_creator_profile_cached", lambda uid, _r=None: ok_creator
    )
    monkeypatch.setattr(profiles_service, "get_brand_profile", lambda uid: ok_brand)
    monkeypatch.setattr(profiles_service, "public_creator", lambda row: row)
    monkeypatch.setattr(profiles_service, "public_brand", lambda row: row)

    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: list(state["cards"]))
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(discover_service, "record_action", lambda **kw: True)
    monkeypatch.setattr(
        discover_service,
        "get_opportunity_cards",
        lambda ids: {i: _view_card() for i in ids if i == LISTING_ID},
    )

    monkeypatch.setattr(jobs, "get", lambda lid: _listing() if lid == LISTING_ID else None)
    monkeypatch.setattr(jobs, "list_by_poster", lambda uid, *, limit=200: list(state["owned"]))
    monkeypatch.setattr(network, "get_connection_between", lambda a, b: None)
    monkeypatch.setattr(
        job_applications, "has_applied", lambda lid, uid: (lid, uid) in state["applied"]
    )
    monkeypatch.setattr(
        job_applications,
        "list_for_applicant",
        lambda uid, *, limit=100: [
            {"listing_id": lid, "created_at": "2026-03-04T10:00:00Z"}
            for (lid, u) in state["applied"]
            if u == uid
        ],
    )
    monkeypatch.setattr(
        job_applications, "count_by_listing", lambda ids: dict.fromkeys(ids, 2)
    )
    return state


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, role: str) -> str:
    uid = str(uuid4())
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])
    return uid


def _card_links(html: str) -> list[str]:
    return re.findall(r'<a class="discover-card-hit" href="([^"]*)"', html)


# --------------------------------------------------- the link destinations


def test_creator_explore_opportunity_card_links_to_creator_detail(client, world):
    _sign_in(client, "creator")
    r = client.get("/creator/discover?kind=opportunity")
    assert r.status_code == 200
    assert _card_links(r.text) == [f"/creator/jobs/{LISTING_ID}"]


def test_brand_explore_opportunity_card_links_to_brand_detail(client, world):
    _sign_in(client, "brand")
    r = client.get("/brand/discover?kind=opportunity")
    assert r.status_code == 200
    assert _card_links(r.text) == [f"/brand/discover/opportunity/{LISTING_ID}"]
    # and nothing on the page still points a brand at the creator-only route
    assert f"/creator/jobs/{LISTING_ID}" not in r.text


def test_link_is_a_real_path_not_collapsed_to_a_hash(client, world):
    """``safe_url`` turns relative paths into '#'; the link must not go
    through it or the whole card becomes a dead tap target."""
    _sign_in(client, "creator")
    assert "#" not in _card_links(client.get("/creator/discover?kind=opportunity").text)
    _sign_in(client, "brand")
    assert "#" not in _card_links(client.get("/brand/discover?kind=opportunity").text)


def test_card_without_detail_path_falls_back_to_the_creator_route(client, world):
    """Defensive fallback preserves the pre-fix behavior for any card that
    arrives without a detail_path."""
    card = _view_card()
    card.pop("detail_path")
    world["cards"] = [card]
    _sign_in(client, "creator")
    r = client.get("/creator/discover?kind=opportunity")
    assert _card_links(r.text) == [f"/creator/jobs/{LISTING_ID}"]


# ------------------------------------------- following the link works


def test_creator_can_open_the_linked_detail_page(client, world):
    _sign_in(client, "creator")
    href = _card_links(client.get("/creator/discover?kind=opportunity").text)[0]
    detail = client.get(href)
    assert detail.status_code == 200
    assert TITLE in detail.text


def test_brand_can_open_the_linked_detail_page_without_a_403(client, world):
    _sign_in(client, "brand")
    href = _card_links(client.get("/brand/discover?kind=opportunity").text)[0]
    detail = client.get(href)
    assert detail.status_code == 200, detail.text[:200]
    assert detail.status_code != 403
    assert TITLE in detail.text


# ------------------------ authorization was NOT loosened to make this work


def test_brand_is_still_refused_by_the_creator_only_detail_route(client, world):
    _sign_in(client, "brand")
    r = client.get(f"/creator/jobs/{LISTING_ID}")
    assert r.status_code == 403


def test_creator_is_still_refused_by_the_brand_only_detail_route(client, world):
    _sign_in(client, "creator")
    r = client.get(f"/brand/discover/opportunity/{LISTING_ID}")
    assert r.status_code in (302, 303, 401, 403)


def test_anonymous_visitor_still_cannot_open_either_detail_route(client, world):
    client.cookies.clear()
    for url in (
        f"/creator/jobs/{LISTING_ID}",
        f"/brand/discover/opportunity/{LISTING_ID}",
    ):
        assert client.get(url).status_code in (302, 303, 401, 403)


# ----------------------------------- "my opportunities" links still work


def test_creator_my_opportunities_link_still_resolves(client, world):
    me = _sign_in(client, "creator")
    world["applied"].add((LISTING_ID, me))
    page = client.get("/creator/discover?kind=opportunity&view=mine")
    assert _card_links(page.text) == [f"/creator/jobs/{LISTING_ID}"]
    detail = client.get(_card_links(page.text)[0])
    assert detail.status_code == 200
    assert "✓ Applied" in detail.text  # Step 5B state intact


def test_brand_my_opportunities_link_still_resolves(client, world):
    me = _sign_in(client, "brand")
    row = _listing()
    row["poster_user_id"] = me
    world["owned"] = [row]
    page = client.get("/brand/discover?kind=opportunity&view=mine")
    assert _card_links(page.text) == [f"/brand/discover/opportunity/{LISTING_ID}"]
    assert "2 applicants" in page.text  # Step 5C count intact


# --------------------------------------------- unrelated behavior intact


def test_explore_cards_keep_their_interested_form_and_markup(client, world):
    _sign_in(client, "creator")
    r = client.get("/creator/discover?kind=opportunity")
    assert 'name="action" value="interested"' in r.text
    assert ">interested</button>" in r.text
    assert "discover-card-mine" not in r.text
    assert "discover-card-status" not in r.text
