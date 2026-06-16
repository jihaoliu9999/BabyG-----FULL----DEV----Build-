"""Profile-views tests — record on view + tier-gated read."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import abuse as abuse_module
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import network as network_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module
from app.services import views as views_module


class FakeWorld:
    def __init__(self):
        self.creators: dict[str, dict[str, Any]] = {}
        self.recorded: list[tuple[str, str]] = []     # (viewer, viewed)
        self.count = 0
        self.viewers: list[dict[str, Any]] = []


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    def _record_view(*, viewer_id, viewed_id):
        if viewer_id == viewed_id:
            return False
        w.recorded.append((viewer_id, viewed_id))
        return True

    monkeypatch.setattr(views_module, "record_view", _record_view)
    monkeypatch.setattr(views_module, "count_distinct_viewers", lambda uid: w.count)
    monkeypatch.setattr(views_module, "list_recent_viewers", lambda uid: w.viewers)

    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        network_module, "list_directory_for_creator",
        lambda uid: [c for c in w.creators.values() if c["user_id"] != uid],
    )
    monkeypatch.setattr(
        network_module, "get_connection_between", lambda a, b: None
    )
    monkeypatch.setattr(network_module, "list_incoming_pending", lambda uid: [])

    # Quiet everything else used by the dashboard / network surface
    monkeypatch.setattr(notifications_module, "create", lambda **kw: True)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "feedback_for_user", lambda uid, ids: {})
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)
    return w


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def _signed_in(client, *, role, user_id):
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _add_creator(world, *, user_id, tier="basic"):
    world.creators[user_id] = {
        "user_id": user_id,
        "full_name": f"Creator {user_id}",
        "instagram_handle": user_id,
        "niches": ["food"],
        "tier": tier,
        "follower_range": "10-50k",
        "onboarding_completed_at": "2026-05-07T00:00:00Z",
        "engagement_range": None, "creator_tenure": None,
        "location_city": None, "location_region": None, "primary_platform": "Instagram",
        "content_formats": ["reels"], "hard_limits": [], "bio": None,
    }


def test_view_recorded_on_network_profile_open(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    _add_creator(world, user_id="c-1")
    _add_creator(world, user_id="c-2")
    r = client.get("/creator/network/c-2")
    assert r.status_code == 200
    assert ("c-1", "c-2") in world.recorded


def test_views_page_basic_tier_shows_upgrade_prompt(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    _add_creator(world, user_id="c-1", tier="basic")
    world.count = 7
    r = client.get("/creator/views")
    assert r.status_code == 200
    # Basic shouldn't see the count. Assert against the specific markup
    # the count would render into — a bare "7" can collide with the
    # asset-hash query string in `app.css?v=…`.
    assert "views-count-card" not in r.text
    assert "<strong>7</strong>" not in r.text
    assert "higher tiers" in r.text


def test_views_page_pro_tier_shows_count(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    _add_creator(world, user_id="c-1", tier="pro")
    world.count = 7
    r = client.get("/creator/views")
    assert r.status_code == 200
    assert ">7</strong>" in r.text


def test_views_page_vip_lists_viewers(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    _add_creator(world, user_id="c-1", tier="vip")
    _add_creator(world, user_id="c-7")
    world.count = 1
    world.viewers = [{"viewer_id": "c-7", "viewed_at": "2026-05-07T00:00:00Z"}]
    r = client.get("/creator/views")
    assert r.status_code == 200
    assert "Creator c-7" in r.text


def test_views_redirects_when_not_onboarded(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    # Not added → no creator_profile row at all
    r = client.get("/creator/views")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/creator"


def test_views_requires_creator(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/views")
    assert r.status_code == 403
