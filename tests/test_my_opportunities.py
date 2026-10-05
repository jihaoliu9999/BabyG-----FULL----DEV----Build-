"""Step 5C: Discover → Opportunities → explore / my opportunities.

Route + template tests run against service-boundary fakes (no live DB,
no external APIs). A second group exercises the REAL service functions
against a recording fake Supabase client so the exact query shapes —
especially "the application ``message`` column is never selected" — are
verified rather than assumed.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import job_applications, jobs, my_opportunities
from app.services import profiles as profiles_service

SECRET_MESSAGE = "PRIVATE-APPLICATION-MESSAGE-SENTINEL-9f3a"


# ---------------------------------------------------------------- world


class _World:
    def __init__(self) -> None:
        self.creators: dict[str, dict[str, Any]] = {}
        self.brands: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, dict[str, Any]] = {}
        self.applications: list[dict[str, Any]] = []
        # Call recorders.
        self.list_cards_calls: list[dict[str, Any]] = []
        self.recorded_actions: list[dict[str, Any]] = []
        self.list_for_applicant_calls: list[str] = []
        self.count_requests: list[list[str]] = []
        self.count_unknown = False
        self.explore_cards: list[dict[str, Any]] = []

    def add_creator(self, uid: str, name: str = "Alex Creator") -> None:
        self.creators[uid] = {
            "user_id": uid,
            "full_name": name,
            "instagram_handle": uid[:6],
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "niches": [],
            "primary_platform": "Instagram",
        }

    def add_brand(self, uid: str, name: str = "Olipop") -> None:
        self.brands[uid] = {
            "user_id": uid,
            "company_name": name,
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "location_city": "Austin",
            "location_region": "TX",
            "niche_preferences": [],
        }

    def add_listing(self, poster: str, **kw: Any) -> dict[str, Any]:
        lid = kw.pop("id", None) or str(uuid4())
        row = {
            "id": lid,
            "poster_user_id": poster,
            "poster_role": kw.pop("poster_role", "brand"),
            "title": kw.pop("title", "Summer reel pack"),
            "description": kw.pop("description", "Four short-form reels."),
            "listing_type": "brand_deal",
            "compensation_text": kw.pop("compensation_text", "$2k"),
            "budget_min": None,
            "budget_max": None,
            "target_niches": kw.pop("target_niches", ["fashion"]),
            "deadline": None,
            "is_active": kw.pop("is_active", True),
            "is_taken_down": kw.pop("is_taken_down", False),
            "location_city": kw.pop("location_city", None),
            "location_region": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
        row.update(kw)
        self.listings[lid] = row
        return row

    def apply(self, listing_id: str, applicant: str, when: str = "2026-03-04T10:00:00Z") -> None:
        self.applications.append(
            {
                "listing_id": listing_id,
                "applicant_user_id": applicant,
                "message": SECRET_MESSAGE,
                "created_at": when,
            }
        )


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()

    monkeypatch.setattr(
        profiles_service, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        profiles_service, "get_creator_profile_cached", lambda uid, _r=None: w.creators.get(uid)
    )
    monkeypatch.setattr(profiles_service, "get_brand_profile", lambda uid: w.brands.get(uid))
    monkeypatch.setattr(profiles_service, "public_creator", lambda row: row)
    monkeypatch.setattr(profiles_service, "public_brand", lambda row: row)

    # ---- Explore (existing feed) ----
    def _list_cards(**kwargs: Any) -> list[dict[str, Any]]:
        w.list_cards_calls.append(kwargs)
        return w.explore_cards

    monkeypatch.setattr(discover_service, "list_cards", _list_cards)
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(
        discover_service,
        "record_action",
        lambda **kw: w.recorded_actions.append(kw) or True,
    )

    # ---- live opportunity cards (what the discovery_cards view returns) ----
    def _get_opportunity_cards(ids: list[str]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for lid in ids:
            row = w.listings.get(lid)
            if not row or not row["is_active"] or row["is_taken_down"]:
                continue  # the view only exposes live listings
            poster = row["poster_user_id"]
            name = (w.brands.get(poster) or w.creators.get(poster) or {}).get(
                "company_name"
            ) or (w.creators.get(poster) or {}).get("full_name")
            out[lid] = {
                "card_kind": "opportunity",
                "card_id": lid,
                "owner_user_id": poster,
                "title": row["title"],
                "subtitle": name,
                "location_label": "Austin, TX",
                "tags": list(row["target_niches"]),
                "description": row["description"],
                "compensation_text": row["compensation_text"],
                "budget_min": row["budget_min"],
                "budget_max": row["budget_max"],
                "deadline": row["deadline"],
                "detail_path": f"/creator/jobs/{lid}",
            }
        return out

    monkeypatch.setattr(discover_service, "get_opportunity_cards", _get_opportunity_cards)

    # ---- step 5B applications (fakes mimic the real, message-free shapes) ----
    def _list_for_applicant(uid: str, *, limit: int = 100) -> list[dict[str, Any]]:
        w.list_for_applicant_calls.append(uid)
        rows = [a for a in w.applications if a["applicant_user_id"] == uid]
        rows.sort(key=lambda a: a["created_at"], reverse=True)
        return [
            {"listing_id": a["listing_id"], "created_at": a["created_at"]}
            for a in rows[:limit]
        ]

    def _count_by_listing(ids: list[str]) -> dict[str, int] | None:
        w.count_requests.append(list(ids))
        if w.count_unknown:
            return None
        counts = dict.fromkeys(ids, 0)
        for a in w.applications:
            if a["listing_id"] in counts:
                counts[a["listing_id"]] += 1
        return counts

    monkeypatch.setattr(job_applications, "list_for_applicant", _list_for_applicant)
    monkeypatch.setattr(job_applications, "count_by_listing", _count_by_listing)
    monkeypatch.setattr(
        job_applications,
        "has_applied",
        lambda lid, uid: any(
            a["listing_id"] == lid and a["applicant_user_id"] == uid for a in w.applications
        ),
    )

    # ---- jobs service ----
    monkeypatch.setattr(jobs, "get", lambda lid: w.listings.get(lid))
    monkeypatch.setattr(
        jobs,
        "list_by_poster",
        lambda uid, *, limit=200: [
            r for r in w.listings.values() if r["poster_user_id"] == uid
        ][:limit],
    )
    return w


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, uid: str, role: str = "creator") -> str:
    # The session middleware refreshes the cookie on each response; clear
    # the jar so switching identity mid-test really switches identity.
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return uid


def _explore_card(title: str = "Explore-only opportunity") -> dict[str, Any]:
    cid = str(uuid4())
    return {
        "card_kind": "opportunity",
        "card_id": cid,
        "owner_user_id": str(uuid4()),
        "title": title,
        "subtitle": "Some Brand",
        "tags": ["fashion"],
        "description": "In the feed.",
        "compensation_text": "$1k",
        "budget_min": None,
        "budget_max": None,
        "deadline": None,
        "location_label": None,
        "detail_path": f"/creator/jobs/{cid}",
        "relevance_reasons": [],
    }


def _cards(html: str) -> list[str]:
    return re.findall(r'<article class="discover-card[^"]*"', html)


# ------------------------------------------------- explore / secondary nav


def test_opportunities_defaults_to_explore(client, world):
    _sign_in(client, str(uuid4()))
    world.explore_cards = [_explore_card("Feed opportunity")]
    r = client.get("/creator/discover?kind=opportunity")
    assert r.status_code == 200
    assert "Feed opportunity" in r.text
    assert len(world.list_cards_calls) == 1
    # explore is the active secondary tab
    m = re.search(r'<a href="[^"]*" class="active" aria-current="page">explore</a>', r.text)
    assert m, "explore should be the active secondary tab"
    assert world.list_for_applicant_calls == []


def test_explore_preserves_existing_discovery_behavior(client, world):
    _sign_in(client, str(uuid4()))
    world.explore_cards = [_explore_card("Feed opportunity")]
    r = client.get(
        "/creator/discover?kind=opportunity&category=fashion&location=austin"
        "&budget_min=500&budget_max=2000"
    )
    assert r.status_code == 200
    call = world.list_cards_calls[0]
    assert call["kind"] == "opportunity"
    assert call["category"] == "fashion"
    assert call["location"] == "austin"
    assert call["budget_min"] == 500
    assert call["budget_max"] == 2000
    # the existing swipe form + "interested" action are still on Explore cards
    assert 'name="action" value="interested"' in r.text
    assert ">interested</button>" in r.text
    # and Explore still records its impression of the top card
    assert world.recorded_actions and world.recorded_actions[0]["action_type"] == "viewed"


def test_explore_card_link_is_exactly_what_it_was_before_step_5c(client, world):
    """Step 5C changes the link only in `mine` mode; the live Explore card
    keeps its Step 5A link byte-for-byte."""
    _sign_in(client, str(uuid4()))
    card = _explore_card("Feed opportunity")
    world.explore_cards = [card]
    r = client.get("/creator/discover?kind=opportunity")
    assert (
        f'<a class="discover-card-hit" href="/creator/jobs/{card["card_id"]}" '
        f'aria-label="view opportunity: Feed opportunity"></a>'
    ) in r.text
    assert "discover-card-mine" not in r.text
    assert "discover-card-status" not in r.text


@pytest.mark.parametrize("kind", ["creator", "brand", "all"])
def test_secondary_nav_only_appears_for_opportunities(client, world, kind):
    _sign_in(client, str(uuid4()))
    r = client.get(f"/creator/discover?kind={kind}")
    assert r.status_code == 200
    assert "discover-sub-tabs" not in r.text
    assert "my opportunities" not in r.text


def test_secondary_nav_renders_both_tabs_on_opportunities(client, world):
    _sign_in(client, str(uuid4()))
    r = client.get("/creator/discover?kind=opportunity")
    assert 'class="discover-sub-tabs"' in r.text
    assert ">explore</a>" in r.text
    assert ">my opportunities</a>" in r.text
    # it is a second row, not a replacement for the main tabs
    assert ">creators</a>" in r.text and ">brands</a>" in r.text and ">opportunities</a>" in r.text


def test_view_param_is_ignored_outside_opportunities(client, world):
    uid = _sign_in(client, str(uuid4()))
    world.add_creator(uid)
    r = client.get("/creator/discover?kind=creator&view=mine")
    assert r.status_code == 200
    assert len(world.list_cards_calls) == 1  # normal feed, not the personal list
    assert world.list_for_applicant_calls == []


def test_unknown_view_value_falls_back_to_explore(client, world):
    _sign_in(client, str(uuid4()))
    world.explore_cards = [_explore_card("Feed opportunity")]
    r = client.get("/creator/discover?kind=opportunity&view=banana")
    assert r.status_code == 200
    assert "Feed opportunity" in r.text
    assert world.list_for_applicant_calls == []


def test_sub_tab_links_carry_filters_and_view_state(client, world):
    _sign_in(client, str(uuid4()))
    r = client.get("/creator/discover?kind=opportunity&category=fashion&view=mine")
    assert 'href="/creator/discover?kind=opportunity&category=fashion"' in r.text
    assert 'href="/creator/discover?kind=opportunity&view=mine&category=fashion"' in r.text
    # the filter form keeps the user inside "my opportunities"
    assert '<input type="hidden" name="view" value="mine" />' in r.text


def test_explore_filter_form_has_no_view_field(client, world):
    _sign_in(client, str(uuid4()))
    r = client.get("/creator/discover?kind=opportunity")
    assert 'name="view"' not in r.text


def test_existing_header_controls_survive_in_both_views(client, world):
    uid = _sign_in(client, str(uuid4()))
    world.add_creator(uid)
    for url in (
        "/creator/discover?kind=opportunity",
        "/creator/discover?kind=opportunity&view=mine",
    ):
        r = client.get(url)
        assert 'href="/creator/opportunities/new">+ post</a>' in r.text
        assert "data-filter-toggle" in r.text


# --------------------------------------------------------- creator: mine


def test_creator_can_open_my_opportunities(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster, title="Applied reel pack")
    world.apply(listing["id"], me)

    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert r.status_code == 200
    assert "Applied reel pack" in r.text
    assert "✓ Applied" in r.text
    assert world.list_cards_calls == []  # not the Explore feed
    assert world.recorded_actions == []  # not a discovery impression
    assert re.search(
        r'class="active" aria-current="page">my opportunities</a>', r.text
    )


def test_creator_sees_only_opportunities_they_applied_to(client, world):
    me = _sign_in(client, str(uuid4()))
    other = str(uuid4())
    poster = str(uuid4())
    world.add_creator(me)
    world.add_creator(other)
    world.add_brand(poster)
    mine = world.add_listing(poster, title="I applied here")
    theirs = world.add_listing(poster, title="Someone else applied here")
    world.add_listing(poster, title="Nobody applied here")
    world.apply(mine["id"], me)
    world.apply(theirs["id"], other)

    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert "I applied here" in r.text
    assert "Someone else applied here" not in r.text
    assert "Nobody applied here" not in r.text
    assert len(_cards(r.text)) == 1


def test_creator_does_not_see_another_creators_applications(client, world):
    me = _sign_in(client, str(uuid4()))
    other = str(uuid4())
    poster = str(uuid4())
    world.add_creator(me)
    world.add_creator(other)
    world.add_brand(poster)
    listing = world.add_listing(poster, title="Only the other creator applied")
    world.apply(listing["id"], other)

    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert "Only the other creator applied" not in r.text
    assert "No applications yet" in r.text


def test_creator_identity_comes_from_session_not_query_string(client, world):
    me = _sign_in(client, str(uuid4()))
    victim = str(uuid4())
    poster = str(uuid4())
    world.add_creator(me)
    world.add_creator(victim)
    world.add_brand(poster)
    listing = world.add_listing(poster, title="Victim's application")
    world.apply(listing["id"], victim)

    r = client.get(
        f"/creator/discover?kind=opportunity&view=mine&user_id={victim}"
        f"&applicant_user_id={victim}&creator_id={victim}"
    )
    assert "Victim&#39;s application" not in r.text and "Victim's application" not in r.text
    assert world.list_for_applicant_calls == [me]


def test_creator_applied_state_and_date_come_from_step_5b_records(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster, title="Dated application")
    world.apply(listing["id"], me, when="2026-03-04T10:00:00Z")

    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert "✓ Applied" in r.text
    assert "mar 4, 2026" in r.text
    # withdraw the underlying record → the item disappears: nothing is cached
    world.applications.clear()
    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert "Dated application" not in r.text


def test_creator_most_recent_application_first(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    older = world.add_listing(poster, title="Older application")
    newer = world.add_listing(poster, title="Newer application")
    world.apply(older["id"], me, when="2026-02-01T00:00:00Z")
    world.apply(newer["id"], me, when="2026-03-01T00:00:00Z")
    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert r.text.index("Newer application") < r.text.index("Older application")


def test_creator_item_links_to_existing_step_5a_detail_which_shows_applied(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster, title="Open me")
    world.apply(listing["id"], me)

    r = client.get("/creator/discover?kind=opportunity&view=mine")
    href = f'href="/creator/jobs/{listing["id"]}"'
    assert href in r.text
    assert "discover-card-hit" in r.text

    detail = client.get(f"/creator/jobs/{listing['id']}")
    assert detail.status_code == 200
    assert "Open me" in detail.text
    assert "✓ Applied" in detail.text  # Step 5B state unchanged


def test_creator_listing_no_longer_viewable_is_omitted_not_dead_linked(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    live = world.add_listing(poster, title="Still live")
    closed = world.add_listing(poster, title="Since closed", is_active=False)
    world.apply(live["id"], me)
    world.apply(closed["id"], me)
    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert "Still live" in r.text
    assert "Since closed" not in r.text


def test_creator_mine_has_no_swipe_form_and_no_action_buttons(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster)
    world.apply(listing["id"], me)
    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert "/creator/discover/swipe" not in r.text
    assert ">interested</button>" not in r.text
    assert 'name="action"' not in r.text


def test_creator_empty_state(client, world):
    me = _sign_in(client, str(uuid4()))
    world.add_creator(me)
    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert r.status_code == 200
    assert "No applications yet" in r.text
    assert "Opportunities you apply to will appear here." in r.text
    assert 'href="/creator/discover?kind=opportunity">Explore opportunities</a>' in r.text
    assert _cards(r.text) == []  # no fake/demo content


# ---------------------------------------------------------- brand: mine


def _brand(client, world) -> str:
    uid = _sign_in(client, str(uuid4()), role="brand")
    world.add_brand(uid)
    return uid


def test_brand_can_open_my_opportunities(client, world):
    me = _brand(client, world)
    world.add_listing(me, title="Our campaign")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert r.status_code == 200
    assert "Our campaign" in r.text
    assert world.list_cards_calls == []
    assert world.recorded_actions == []


def test_brand_sees_only_opportunities_it_owns(client, world):
    me = _brand(client, world)
    other = str(uuid4())
    world.add_brand(other, "Rival Co")
    world.add_listing(me, title="Mine")
    world.add_listing(other, title="Rival listing")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "Mine" in r.text
    assert "Rival listing" not in r.text
    assert len(_cards(r.text)) == 1


def test_brand_does_not_see_another_users_posts(client, world):
    me = _brand(client, world)
    creator = str(uuid4())
    world.add_creator(creator)
    world.add_listing(creator, poster_role="creator", title="A creator's post")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "A creator&#39;s post" not in r.text and "A creator's post" not in r.text
    assert "No opportunities posted yet" in r.text
    _ = me


def test_brand_ownership_ignores_poster_role_label(client, world):
    """Rows written before poster_role persisted carry a default role even
    when a brand posted them; ownership is poster_user_id."""
    me = _brand(client, world)
    world.add_listing(me, poster_role="creator", title="Legacy labelled post")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "Legacy labelled post" in r.text


def test_brand_sees_correct_real_applicant_count(client, world):
    me = _brand(client, world)
    listing = world.add_listing(me, title="Counted")
    for _ in range(3):
        world.apply(listing["id"], str(uuid4()))
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "3 applicants" in r.text


def test_zero_applications_shows_zero_applicants(client, world):
    me = _brand(client, world)
    world.add_listing(me, title="Nobody yet")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "0 applicants" in r.text


def test_applicant_count_singular_and_multiple_per_listing(client, world):
    me = _brand(client, world)
    one = world.add_listing(me, title="One applicant listing")
    many = world.add_listing(me, title="Many applicants listing")
    world.apply(one["id"], str(uuid4()))
    for _ in range(5):
        world.apply(many["id"], str(uuid4()))
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "1 applicant<" in r.text
    assert "1 applicants" not in r.text
    assert "5 applicants" in r.text


def test_applicant_count_requested_only_for_owned_listings(client, world):
    me = _brand(client, world)
    other = str(uuid4())
    world.add_brand(other)
    mine = world.add_listing(me)
    theirs = world.add_listing(other)
    world.apply(theirs["id"], str(uuid4()))
    client.get("/brand/discover?kind=opportunity&view=mine")
    requested = {i for batch in world.count_requests for i in batch}
    assert requested == {mine["id"]}
    assert theirs["id"] not in requested


def test_unknown_count_is_omitted_never_shown_as_zero(client, world):
    me = _brand(client, world)
    world.count_unknown = True
    world.add_listing(me, title="Count unavailable")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert r.status_code == 200
    assert "Count unavailable" in r.text
    # no applicant COUNT label of any kind (the card's link path now
    # legitimately contains "/applicants" -- Step 5D -- so look at labels)
    assert re.search(r"\b\d+ applicants?\b", r.text) is None
    assert "discover-card-status-main" not in r.text


def test_brand_taken_down_listing_excluded_closed_listing_marked(client, world):
    me = _brand(client, world)
    world.add_listing(me, title="Removed by operator", is_taken_down=True)
    world.add_listing(me, title="Closed by us", is_active=False)
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "Removed by operator" not in r.text
    assert "Closed by us" in r.text
    assert ">closed</span>" in r.text


def test_brand_card_links_to_applicant_review_and_list_has_no_review_controls(client, world):
    me = _brand(client, world)
    listing = world.add_listing(me, title="Tap me")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    # Step 5D: a POSTED opportunity opens its applicant review...
    assert f'href="/brand/discover/opportunity/{listing["id"]}/applicants"' in r.text
    assert f'href="/brand/discover/opportunity/{listing["id"]}"' not in r.text
    # ...but the list itself stays a plain list: no applicant names, no
    # review/offer controls
    for forbidden in ("shortlist", "reject", "accept", "offer"):
        assert forbidden not in r.text.lower().replace("opportunities", "")


def test_brand_empty_state_uses_existing_post_flow(client, world):
    _brand(client, world)
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert "No opportunities posted yet" in r.text
    assert "Opportunities you post will appear here." in r.text
    assert 'href="/brand/campaigns/new">Post an opportunity</a>' in r.text
    assert _cards(r.text) == []


def test_brand_explore_unchanged_and_secondary_nav_present(client, world):
    _brand(client, world)
    world.explore_cards = [_explore_card("Brand feed card")]
    r = client.get("/brand/discover?kind=opportunity")
    assert r.status_code == 200
    assert "Brand feed card" in r.text
    assert len(world.list_cards_calls) == 1
    assert ">my opportunities</a>" in r.text
    r = client.get("/brand/discover?kind=creator")
    assert "discover-sub-tabs" not in r.text


# ------------------------------------------------------- filters in mine


def test_filters_apply_inside_my_opportunities(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    fashion = world.add_listing(poster, title="Fashion one", target_niches=["fashion"])
    food = world.add_listing(poster, title="Food one", target_niches=["food"])
    world.apply(fashion["id"], me)
    world.apply(food["id"], me)
    r = client.get("/creator/discover?kind=opportunity&view=mine&category=food")
    assert "Food one" in r.text and "Fashion one" not in r.text


def test_filtered_to_zero_is_not_reported_as_no_applications(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster, target_niches=["fashion"])
    world.apply(listing["id"], me)
    r = client.get("/creator/discover?kind=opportunity&view=mine&category=nomatch")
    assert "No matches" in r.text
    assert "No applications yet" not in r.text
    assert 'href="/creator/discover?kind=opportunity&view=mine">clear filters</a>' in r.text


def test_apply_filters_semantics_mirror_explore():
    items = [
        {"tags": ["Fashion"], "location_label": "Austin, TX", "budget_min": 500, "budget_max": 1500},
        {"tags": ["food"], "location_label": "Miami, FL", "budget_min": None, "budget_max": None},
    ]
    f = my_opportunities.apply_filters
    assert len(f(items, category=None, location=None, budget_min=None, budget_max=None)) == 2
    assert len(f(items, category="fashion", location=None, budget_min=None, budget_max=None)) == 1
    assert len(f(items, category=None, location="miami", budget_min=None, budget_max=None)) == 1
    # a stated budget bound excludes listings with no budget (as Explore does)
    assert len(f(items, category=None, location=None, budget_min=1000, budget_max=None)) == 1
    assert len(f(items, category=None, location=None, budget_min=None, budget_max=600)) == 1
    assert len(f(items, category=None, location=None, budget_min=2000, budget_max=None)) == 0


def test_clean_view_and_applicant_label():
    assert my_opportunities.clean_view("mine") == "mine"
    assert my_opportunities.clean_view(" MINE ") == "mine"
    assert my_opportunities.clean_view("explore") == "explore"
    assert my_opportunities.clean_view(None) == "explore"
    assert my_opportunities.clean_view("../etc") == "explore"
    assert my_opportunities.applicant_label(0) == "0 applicants"
    assert my_opportunities.applicant_label(1) == "1 applicant"
    assert my_opportunities.applicant_label(7) == "7 applicants"
    assert my_opportunities.applicant_label(None) == ""


def test_poster_items_defends_against_a_service_returning_foreign_rows(monkeypatch):
    me, other = str(uuid4()), str(uuid4())
    mine = {"id": str(uuid4()), "poster_user_id": me, "title": "mine", "is_active": True}
    foreign = {"id": str(uuid4()), "poster_user_id": other, "title": "foreign", "is_active": True}
    monkeypatch.setattr(jobs, "list_by_poster", lambda uid, *, limit=200: [mine, foreign])
    seen: list[list[str]] = []
    monkeypatch.setattr(
        job_applications, "count_by_listing", lambda ids: seen.append(ids) or dict.fromkeys(ids, 0)
    )
    items = my_opportunities.poster_items(me, detail_prefix="/x/")
    assert [i["title"] for i in items] == ["mine"]
    assert seen == [[mine["id"]]]


# --------------------------------------------------------- privacy/scope


def test_application_messages_never_rendered_in_either_role(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster, title="Has a private message")
    world.apply(listing["id"], me)
    creator_html = client.get("/creator/discover?kind=opportunity&view=mine").text
    assert "Has a private message" in creator_html
    assert SECRET_MESSAGE not in creator_html

    # brand side: the poster gets a count, not the message or the applicant
    brand = _sign_in(client, poster, role="brand")
    world.listings[listing["id"]]["poster_user_id"] = brand
    brand_html = client.get("/brand/discover?kind=opportunity&view=mine").text
    assert "1 applicant" in brand_html
    assert SECRET_MESSAGE not in brand_html
    assert me not in brand_html
    assert "Alex Creator" not in brand_html


def test_wrong_role_cannot_use_the_other_roles_route(client, world):
    _sign_in(client, str(uuid4()), role="brand")
    r = client.get("/creator/discover?kind=opportunity&view=mine")
    assert r.status_code in (302, 303, 401, 403)
    _sign_in(client, str(uuid4()), role="creator")
    r = client.get("/brand/discover?kind=opportunity&view=mine")
    assert r.status_code in (302, 303, 401, 403)


def test_my_opportunities_creates_no_dm_offer_deal_or_payment(client, world, monkeypatch):
    import importlib

    guarded = (
        "app.services.dms",
        "app.services.instagram_dms",
        "app.services.babyg_deals",
        "app.services.creator_payouts",
        "app.integrations.stripe_client",
        "app.services.network",
    )
    for mod_name in guarded:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for attr in dir(mod):
            if attr.startswith("_") or not callable(getattr(mod, attr, None)):
                continue
            if mod_name == "app.services.network" and attr in {
                "get_connection_between",
                "list_incoming_pending",
                "list_accepted_for_user",
                "list_directory_for_creator",
            }:
                continue  # read helpers the page chrome may touch
            if mod_name == "app.services.dms" and attr in {
                "unread_count_for_user",
                "list_threads_for_user",
            }:
                continue  # tabbar badge reads, not DM creation

            def _boom(*a: Any, _m: str = mod_name, _a: str = attr, **k: Any) -> None:
                raise AssertionError(f"my opportunities touched {_m}.{_a}")

            monkeypatch.setattr(mod, attr, _boom, raising=False)

    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster)
    world.apply(listing["id"], me)
    assert client.get("/creator/discover?kind=opportunity&view=mine").status_code == 200
    brand = _sign_in(client, poster, role="brand")
    world.listings[listing["id"]]["poster_user_id"] = brand
    world.add_brand(brand)
    assert client.get("/brand/discover?kind=opportunity&view=mine").status_code == 200


def test_my_opportunities_service_reads_no_tables_directly():
    """It only composes existing services; no new table, no schema."""
    src = Path("app/services/my_opportunities.py").read_text(encoding="utf-8")
    assert ".table(" not in src
    assert "supabase" not in src.lower()


def test_no_migration_is_part_of_step_5c():
    names = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert "0048_creator_job_applications.sql" in names
    assert not any("my_opportunit" in n or "tracking" in n for n in names)


def test_step_5b_submission_unchanged_and_detail_applied_state(client, world):
    me = _sign_in(client, str(uuid4()))
    poster = str(uuid4())
    world.add_creator(me)
    world.add_brand(poster)
    listing = world.add_listing(poster)
    detail = client.get(f"/creator/jobs/{listing['id']}")
    assert f'href="/creator/jobs/{listing["id"]}/apply"' in detail.text
    assert "✓ Applied" not in detail.text
    world.apply(listing["id"], me)
    detail = client.get(f"/creator/jobs/{listing['id']}")
    assert "✓ Applied" in detail.text


# ------------------------------- real services vs a recording fake client


class _Recorder:
    """Records every chained PostgREST call; ``execute`` replays pages."""

    def __init__(self, pages: list[list[dict[str, Any]]] | None = None, error: bool = False):
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.pages = list(pages or [[]])
        self.error = error
        self.tables: list[str] = []

    def table(self, name: str) -> _Recorder:
        self.tables.append(name)
        return self

    def __getattr__(self, name: str):  # select/eq/in_/order/range/limit...
        def _call(*args: Any, **kwargs: Any) -> _Recorder:
            self.calls.append((name, args))
            return self

        return _call

    def execute(self) -> SimpleNamespace:
        if self.error:
            raise PostgrestAPIError({"message": "boom", "code": "500", "hint": None, "details": None})
        data = self.pages.pop(0) if len(self.pages) > 1 else self.pages[0]
        return SimpleNamespace(data=data)

    def args(self, name: str) -> list[tuple[Any, ...]]:
        return [a for n, a in self.calls if n == name]


def _use(monkeypatch: pytest.MonkeyPatch, rec: _Recorder) -> _Recorder:
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: rec)
    return rec


def test_list_for_applicant_never_selects_the_message_column(monkeypatch):
    uid = str(uuid4())
    rec = _use(monkeypatch, _Recorder([[{"listing_id": str(uuid4()), "created_at": "x"}]]))
    rows = job_applications.list_for_applicant(uid)
    assert rec.tables == ["creator_job_applications"]
    assert rec.args("select") == [("listing_id,created_at",)]
    assert "message" not in rec.args("select")[0][0]
    assert ("applicant_user_id", uid) in rec.args("eq")
    assert len(rows) == 1


def test_list_for_applicant_rejects_non_uuid_and_survives_db_error(monkeypatch):
    rec = _use(monkeypatch, _Recorder(error=True))
    assert job_applications.list_for_applicant("not-a-uuid") == []
    assert rec.calls == []  # never queried with a malformed identity
    assert job_applications.list_for_applicant(str(uuid4())) == []


def test_count_by_listing_counts_real_rows_with_explicit_zeros(monkeypatch):
    a, b = str(uuid4()), str(uuid4())
    rows = [{"listing_id": a}, {"listing_id": a}, {"listing_id": a}]
    rec = _use(monkeypatch, _Recorder([rows]))
    assert job_applications.count_by_listing([a, b]) == {a: 3, b: 0}
    assert rec.args("select") == [("listing_id",)]  # ids only — no content


def test_count_by_listing_pages_instead_of_truncating(monkeypatch):
    a = str(uuid4())
    full = [{"listing_id": a}] * 1000
    _use(monkeypatch, _Recorder([full, full, [{"listing_id": a}] * 7]))
    assert job_applications.count_by_listing([a]) == {a: 2007}


def test_count_by_listing_returns_none_on_error_never_zero(monkeypatch):
    _use(monkeypatch, _Recorder(error=True))
    assert job_applications.count_by_listing([str(uuid4())]) is None


def test_count_by_listing_chunks_large_id_lists(monkeypatch):
    ids = [str(uuid4()) for _ in range(120)]
    rec = _use(monkeypatch, _Recorder([[]]))
    counts = job_applications.count_by_listing(ids)
    assert counts is not None and len(counts) == 120
    assert [len(a[1]) for a in rec.args("in_")] == [50, 50, 20]


def test_get_opportunity_cards_reads_the_public_view_only(monkeypatch):
    cid, owner = str(uuid4()), str(uuid4())
    raw = {
        "card_kind": "opportunity",
        "card_id": cid,
        "owner_user_id": owner,
        "title": "Live one",
        "tags": ["fashion"],
        "created_at": "2026-01-01T00:00:00Z",
    }
    rec = _use(monkeypatch, _Recorder([[raw]]))
    cards = discover_service.get_opportunity_cards([cid, "garbage"])
    assert rec.tables == ["discovery_cards"]
    assert ("card_kind", "opportunity") in rec.args("eq")
    assert cards[cid]["title"] == "Live one"
    assert discover_service.get_opportunity_cards(["garbage"]) == {}
