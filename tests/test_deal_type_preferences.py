"""Deal-type preference persistence + Discover preference-first ordering.

These tests cover the new bits added for the Settings → Deal Preferences
rework:

  * ``profiles.sanitize_deal_type_preferences`` (allowlist + dedupe).
  * ``POST /creator/profile/deals`` accepts the multi-checkbox field and
    persists via ``profiles.update_creator_profile``.
  * ``discover.list_cards`` applies preference-first ordering: matching
    opportunity cards float above non-matching, and every card the
    viewer would otherwise see is still returned.

The Discover ordering tests monkeypatch the supabase read so they run
without a live database. They exercise the pure Python partition step,
which is the piece we own and can regress in isolation from the
``discovery_cards`` view.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import profiles

# ---------- sanitize_deal_type_preferences ----------


def test_sanitize_accepts_known_values_lowercased_and_dedupes():
    out = profiles.sanitize_deal_type_preferences(
        ["Collab", "brand_deal", "collab", "  UGC_GIG "]
    )
    # order preserved, dupes removed, whitespace + case normalized
    assert out == ["collab", "brand_deal", "ugc_gig"]


def test_sanitize_drops_unknown_values_silently():
    """A tampered form should never land a value the DB CHECK rejects."""
    out = profiles.sanitize_deal_type_preferences(
        ["collab", "payment_only", "", "hiring", "not_a_type"]
    )
    assert out == ["collab", "hiring"]


def test_sanitize_none_and_empty_return_empty_list():
    assert profiles.sanitize_deal_type_preferences(None) == []
    assert profiles.sanitize_deal_type_preferences([]) == []


# ---------- POST /creator/profile/deals ----------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, user_id: str | None = None) -> str:
    uid = user_id or str(uuid4())
    response = Response()
    write_session(response, {"user_id": uid, "role": "creator"})
    cookie = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return uid


def test_profile_deals_post_persists_selected_deal_types(client, monkeypatch):
    uid = _signed_in(client)
    monkeypatch.setattr(
        profiles,
        "get_creator_profile",
        lambda _uid: {"user_id": _uid, "onboarding_completed_at": "2026-01-01T00:00:00Z"},
    )
    captured: dict[str, Any] = {}

    def _update(_uid: str, payload: dict[str, Any]) -> bool:
        captured["user_id"] = _uid
        captured["payload"] = payload
        return True

    monkeypatch.setattr(profiles, "update_creator_profile", _update)

    response = client.post(
        "/creator/profile/deals",
        data={"deal_type_preferences": ["collab", "brand_deal"]},
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/creator/profile/settings?deals=ok")
    assert captured["user_id"] == uid
    assert captured["payload"]["deal_type_preferences"] == ["collab", "brand_deal"]
    # Rate floor was removed from the Settings form in a follow-up
    # cleanup; the POST handler must NOT include the column in its
    # payload so historical values are preserved untouched in the DB.
    assert "deal_min_rate_text" not in captured["payload"]


def test_profile_deals_post_empty_clears_preferences(client, monkeypatch):
    """A creator unchecking every box should write an empty list, not
    leave stale values in place."""
    _signed_in(client)
    monkeypatch.setattr(
        profiles,
        "get_creator_profile",
        lambda _uid: {"user_id": _uid, "onboarding_completed_at": "2026-01-01T00:00:00Z"},
    )
    captured: dict[str, Any] = {}

    def _update(_uid: str, payload: dict[str, Any]) -> bool:
        captured["payload"] = payload
        return True

    monkeypatch.setattr(profiles, "update_creator_profile", _update)

    response = client.post("/creator/profile/deals", data={})
    assert response.status_code == 303
    assert captured["payload"]["deal_type_preferences"] == []
    # Rate floor is no longer part of the form. The POST handler must
    # never touch that column so any legacy stored value survives.
    assert "deal_min_rate_text" not in captured["payload"]


def test_profile_deals_post_drops_unknown_types_before_persist(client, monkeypatch):
    _signed_in(client)
    monkeypatch.setattr(
        profiles,
        "get_creator_profile",
        lambda _uid: {"user_id": _uid, "onboarding_completed_at": "2026-01-01T00:00:00Z"},
    )
    captured: dict[str, Any] = {}

    def _update(_uid: str, payload: dict[str, Any]) -> bool:
        captured["payload"] = payload
        return True

    monkeypatch.setattr(profiles, "update_creator_profile", _update)

    response = client.post(
        "/creator/profile/deals",
        data={"deal_type_preferences": ["collab", "payment_only"]},
    )
    assert response.status_code == 303
    assert captured["payload"]["deal_type_preferences"] == ["collab"]


# ---------- discover.list_cards preference-first ordering ----------


def _card(
    kind: str = "opportunity",
    *,
    listing_type: str | None = None,
    title: str,
) -> dict[str, Any]:
    """Row shape matching the ``discovery_cards`` view + migration 0046."""
    return {
        "card_kind": kind,
        "card_id": str(uuid4()),
        "owner_user_id": str(uuid4()),
        "title": title,
        "subtitle": None,
        "image_url": None,
        "location_label": None,
        "tags": ["fashion"],
        "created_at": "2026-06-17T12:00:00Z",
        "description": None,
        "profile_handle": None,
        "follower_range": None,
        "primary_platform": None,
        "verification_status": None,
        "compensation_type": None,
        "compensation_text": None,
        "budget_min": None,
        "budget_max": None,
        "deadline": None,
        "listing_type": listing_type,
        "detail_path": f"/creator/jobs/{uuid4()}",
    }


def _patch_supabase_result(monkeypatch, rows: list[dict[str, Any]]) -> None:
    """Stub the supabase-py fluent chain in ``discover.list_cards`` so
    the query returns ``rows`` and every viewer-scoped filter is a
    no-op. Also stubs the exclusions helper so no history read fires."""
    class _Result:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self.data = data

    class _Query:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self._data = data

        def _self(self, *_args, **_kwargs) -> _Query:
            return self

        select = _self
        neq = _self
        order = _self
        limit = _self
        eq = _self
        contains = _self
        ilike = _self
        gte = _self
        lte = _self

        def execute(self) -> _Result:
            return _Result(self._data)

    class _Client:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self._data = data

        def table(self, _name: str) -> _Query:
            return _Query(self._data)

    from app.core import supabase_client

    monkeypatch.setattr(supabase_client, "get_service_client", lambda: _Client(rows))
    monkeypatch.setattr(discover_service, "_excluded_card_keys", lambda _uid: set())


def test_list_cards_prefers_matching_listing_types(monkeypatch):
    """With saved preferences [collab, brand_deal], opportunity cards
    whose listing_type is one of those must appear first while every
    other card stays visible in its original position."""
    viewer_id = str(uuid4())
    ugc = _card(listing_type="ugc_gig", title="ugc pack")
    collab = _card(listing_type="collab", title="fashion collab")
    hiring = _card(listing_type="hiring", title="video editor")
    brand = _card(listing_type="brand_deal", title="summer campaign")
    creator = _card(kind="creator", title="peer creator")
    _patch_supabase_result(monkeypatch, [ugc, collab, hiring, brand, creator])

    out = discover_service.list_cards(
        viewer_id=viewer_id,
        viewer_role="creator",
        viewer_deal_type_preferences=["collab", "brand_deal"],
        limit=10,
    )
    titles = [c["title"] for c in out]
    # Matching opportunities float above non-matching + non-opportunities,
    # keeping their internal order.
    assert titles == [
        "fashion collab",
        "summer campaign",
        "ugc pack",
        "video editor",
        "peer creator",
    ]


def test_list_cards_without_preferences_keeps_default_order(monkeypatch):
    """No saved preferences ⇒ zero behavior change from before 0044/0045."""
    viewer_id = str(uuid4())
    rows = [
        _card(listing_type="hiring", title="video editor"),
        _card(listing_type="collab", title="fashion collab"),
        _card(kind="creator", title="peer creator"),
    ]
    _patch_supabase_result(monkeypatch, rows)

    out = discover_service.list_cards(
        viewer_id=viewer_id,
        viewer_role="creator",
        viewer_deal_type_preferences=None,
        limit=10,
    )
    assert [c["title"] for c in out] == [
        "video editor",
        "fashion collab",
        "peer creator",
    ]


def test_list_cards_empty_preferences_keeps_default_order(monkeypatch):
    """Empty list must behave exactly like unset — never a hard filter."""
    viewer_id = str(uuid4())
    rows = [
        _card(listing_type="hiring", title="video editor"),
        _card(listing_type="collab", title="fashion collab"),
    ]
    _patch_supabase_result(monkeypatch, rows)

    out = discover_service.list_cards(
        viewer_id=viewer_id,
        viewer_role="creator",
        viewer_deal_type_preferences=[],
        limit=10,
    )
    assert [c["title"] for c in out] == ["video editor", "fashion collab"]


def test_list_cards_preferences_never_drop_valid_cards(monkeypatch):
    """Even when a creator prefers types nothing matches, every card the
    viewer is allowed to see must still be returned — preference is a
    soft ordering signal, not a filter."""
    viewer_id = str(uuid4())
    rows = [
        _card(listing_type="hiring", title="video editor"),
        _card(kind="creator", title="peer creator"),
        _card(kind="brand", title="atelier fig"),
    ]
    _patch_supabase_result(monkeypatch, rows)

    out = discover_service.list_cards(
        viewer_id=viewer_id,
        viewer_role="creator",
        viewer_deal_type_preferences=["ugc_gig"],  # nothing here matches
        limit=10,
    )
    titles = {c["title"] for c in out}
    assert titles == {"video editor", "peer creator", "atelier fig"}
