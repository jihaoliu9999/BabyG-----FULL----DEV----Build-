"""Discover → Creators / Brands: the profile card's full-card link must work.

Regression: the profile card piped its internal ``detail_path`` through
``safe_url``, which (by design, for external links) collapses everything
that is not http(s) to "#". So every creator/brand card on Discover had a
dead ``href="#"`` overlay link.

``detail_path`` is trusted server-side data -- the DB view computes it as a
constant prefix plus a uuid cast, and the brand route rebuilds it from a
``safe_uuid``-validated id -- so the fix renders it through a new
``safe_path`` filter that accepts only same-origin app paths. ``safe_url``
itself is untouched and still guards genuinely external URLs.

Each test follows the rendered link and checks the destination opens.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core import templating
from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import discovery, network, views
from app.services import profiles as profiles_service

TARGET = str(uuid4())
OWNER = str(uuid4())


def _card(kind: str, path: str, **extra: Any) -> dict[str, Any]:
    """A card exactly as the ``discovery_cards`` view yields it (creator-
    shaped paths for every viewer; the brand route rewrites them)."""
    card = {
        "card_kind": kind,
        "card_id": TARGET,
        "owner_user_id": OWNER,
        "title": "Sam Rivera" if kind == "creator" else "Olipop",
        "subtitle": "@sam" if kind == "creator" else "beverage",
        "image_url": None,
        "location_label": "Austin, TX",
        "tags": ["fashion"],
        "description": "Short bio.",
        "profile_handle": None,
        "follower_range": None,
        "primary_platform": None,
        "verification_status": None,
        "compensation_text": None,
        "budget_min": None,
        "budget_max": None,
        "deadline": None,
        "listing_type": None,
        "detail_path": path,
        "relevance_reasons": [],
    }
    card.update(extra)
    return card


CREATOR_CARD_PATH = f"/creator/network/{TARGET}"
BRAND_CARD_PATH = f"/creator/discover/brand/{TARGET}"


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state: dict[str, Any] = {"cards": []}

    peer = {
        "user_id": TARGET,
        "full_name": "Sam Rivera",
        "instagram_handle": "sam",
        "onboarding_completed_at": "2026-01-01T00:00:00Z",
        "niches": ["fashion"],
        "content_formats": [],
        "hard_limits": [],
        "location_city": None,
        "location_region": None,
        "primary_platform": "Instagram",
        "bio": "Short bio.",
    }

    def _creator_profile(uid: str) -> dict[str, Any] | None:
        if uid == TARGET:
            return peer
        return {"user_id": uid, "onboarding_completed_at": "2026-01-01T00:00:00Z", "niches": []}

    monkeypatch.setattr(profiles_service, "get_creator_profile", _creator_profile)
    monkeypatch.setattr(
        profiles_service, "get_creator_profile_cached", lambda uid, _r=None: _creator_profile(uid)
    )
    monkeypatch.setattr(
        profiles_service,
        "get_brand_profile",
        lambda uid: {
            "user_id": uid,
            "company_name": "Olipop",
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "niche_preferences": [],
        },
    )
    monkeypatch.setattr(profiles_service, "public_creator", lambda row: row)
    monkeypatch.setattr(profiles_service, "public_brand", lambda row: row)

    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: list(state["cards"]))
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(discover_service, "record_action", lambda **kw: True)
    # The detail routes read a single card by kind + id.
    monkeypatch.setattr(
        discover_service,
        "get_card",
        lambda *, card_kind, card_id, **kw: (
            _card(card_kind, f"/x/{card_id}") if card_id == TARGET else None
        ),
    )
    monkeypatch.setattr(network, "get_connection_between", lambda a, b: None)
    monkeypatch.setattr(views, "record_view", lambda **kw: True)
    monkeypatch.setattr(discovery, "record_action", lambda **kw: True)
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


def _hits(html: str) -> list[str]:
    return re.findall(r'<a class="discover-card-hit" href="([^"]*)"', html)


# ------------------------------------------- the link is a real path now


def test_creator_tab_profile_card_has_a_real_internal_href(client, world):
    world["cards"] = [_card("creator", CREATOR_CARD_PATH)]
    _sign_in(client, "creator")
    hits = _hits(client.get("/creator/discover?kind=creator").text)
    assert hits == [CREATOR_CARD_PATH]
    assert "#" not in hits


def test_brand_tab_profile_card_has_a_real_internal_href(client, world):
    world["cards"] = [_card("brand", BRAND_CARD_PATH)]
    _sign_in(client, "creator")
    hits = _hits(client.get("/creator/discover?kind=brand").text)
    assert hits == [BRAND_CARD_PATH]
    assert "#" not in hits


def test_brand_viewer_creator_and_brand_cards_use_the_brand_routes(client, world):
    _sign_in(client, "brand")
    world["cards"] = [_card("creator", CREATOR_CARD_PATH)]
    assert _hits(client.get("/brand/discover?kind=creator").text) == [
        f"/brand/discover/creator/{TARGET}"
    ]
    world["cards"] = [_card("brand", BRAND_CARD_PATH)]
    assert _hits(client.get("/brand/discover?kind=brand").text) == [
        f"/brand/discover/brand/{TARGET}"
    ]


# ------------------------------------- following the link reaches the page


def test_following_the_creator_card_link_reaches_the_creator_profile(client, world):
    world["cards"] = [_card("creator", CREATOR_CARD_PATH)]
    _sign_in(client, "creator")
    href = _hits(client.get("/creator/discover?kind=creator").text)[0]
    page = client.get(href)
    assert page.status_code == 200, page.text[:300]
    assert "Sam Rivera" in page.text


def test_following_the_brand_card_link_reaches_the_brand_detail(client, world):
    world["cards"] = [_card("brand", BRAND_CARD_PATH)]
    _sign_in(client, "creator")
    href = _hits(client.get("/creator/discover?kind=brand").text)[0]
    page = client.get(href)
    assert page.status_code == 200, page.text[:300]
    assert "Olipop" in page.text


def test_brand_viewer_can_follow_creator_and_brand_card_links(client, world):
    _sign_in(client, "brand")
    for kind, path in (("creator", CREATOR_CARD_PATH), ("brand", BRAND_CARD_PATH)):
        world["cards"] = [_card(kind, path)]
        href = _hits(client.get(f"/brand/discover?kind={kind}").text)[0]
        page = client.get(href)
        assert page.status_code == 200, (kind, href, page.status_code)


def test_mixed_all_feed_every_card_kind_links_to_its_own_route(client, world):
    opp = _card("opportunity", f"/creator/jobs/{TARGET}", title="Reel pack")
    world["cards"] = [opp, _card("creator", CREATOR_CARD_PATH), _card("brand", BRAND_CARD_PATH)]
    _sign_in(client, "brand")
    assert _hits(client.get("/brand/discover?kind=all").text) == [
        f"/brand/discover/opportunity/{TARGET}",
        f"/brand/discover/creator/{TARGET}",
        f"/brand/discover/brand/{TARGET}",
    ]
    _sign_in(client, "creator")
    assert _hits(client.get("/creator/discover?kind=all").text) == [
        f"/creator/jobs/{TARGET}",
        CREATOR_CARD_PATH,
        BRAND_CARD_PATH,
    ]


# ---------------------------------------------------------- security


HOSTILE = [
    "https://evil.example/phish",
    "http://evil.example",
    "//evil.example/x",
    "/\\evil.example",
    "javascript:alert(1)",
    "data:text/html,<script>alert(1)</script>",
    "mailto:a@b.c",
    " /creator/network/x",
    "/creator/network/x y",
    "/creator/network/x\nSet-Cookie: a=b",
    "creator/network/relative-no-slash",
    "",
]


@pytest.mark.parametrize("bad", HOSTILE)
def test_untrusted_detail_path_never_becomes_a_clickable_link(client, world, bad):
    world["cards"] = [_card("creator", bad)]
    _sign_in(client, "creator")
    html = client.get("/creator/discover?kind=creator").text
    assert _hits(html) == ["#"]
    assert "evil.example" not in html
    assert "javascript:alert" not in html


def test_missing_detail_path_is_inert_not_a_crash(client, world):
    card = _card("creator", CREATOR_CARD_PATH)
    card.pop("detail_path")
    world["cards"] = [card]
    _sign_in(client, "creator")
    r = client.get("/creator/discover?kind=creator")
    assert r.status_code == 200
    assert _hits(r.text) == ["#"]


def test_external_image_url_is_still_guarded_by_safe_url(client, world):
    world["cards"] = [_card("creator", CREATOR_CARD_PATH, image_url="javascript:alert(1)")]
    _sign_in(client, "creator")
    html = client.get("/creator/discover?kind=creator").text
    assert "javascript:alert" not in html
    assert 'src="#"' in html


def test_role_guards_on_the_destination_routes_are_unchanged(client, world):
    _sign_in(client, "brand")
    assert client.get(CREATOR_CARD_PATH).status_code == 403
    assert client.get(BRAND_CARD_PATH).status_code == 403
    _sign_in(client, "creator")
    assert client.get(f"/brand/discover/creator/{TARGET}").status_code in (302, 303, 401, 403)
    assert client.get(f"/brand/discover/brand/{TARGET}").status_code in (302, 303, 401, 403)
    client.cookies.clear()
    for path in (CREATOR_CARD_PATH, BRAND_CARD_PATH, f"/brand/discover/creator/{TARGET}"):
        assert client.get(path).status_code in (302, 303, 401, 403)


# ------------------------------------------------- the filters themselves


@pytest.mark.parametrize(
    "good",
    [
        "/creator/network/abc",
        "/brand/discover/creator/abc",
        "/creator/discover/brand/abc",
        "/creator/jobs/abc?x=1&y=2",
        "/",
    ],
)
def test_safe_path_accepts_internal_paths(good):
    assert templating._safe_path(good) == good


@pytest.mark.parametrize("bad", [*HOSTILE, None, 0])
def test_safe_path_rejects_everything_else(bad):
    assert templating._safe_path(bad) == "#"


def test_safe_path_is_registered_and_safe_url_is_untouched():
    assert templating.templates.env.filters["safe_path"] is templating._safe_path
    # safe_url keeps its exact, original contract (external links only)
    assert templating._safe_url("https://example.com/a") == "https://example.com/a"
    assert templating._safe_url("http://example.com") == "http://example.com"
    assert templating._safe_url("/creator/network/abc") == "#"
    assert templating._safe_url("javascript:alert(1)") == "#"
    assert templating._safe_url("//evil.example") == "#"
    assert templating._safe_url(None) == "#"
