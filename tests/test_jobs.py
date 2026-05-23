"""Job listing tests — creator CRUD + operator takedown.

v1 is creator-only. Brand-side jobs browse (signed in as a verified
brand, hitting /brand/jobs) shipped on the brand-side-v1.5 branch.

Stubs the jobs service; the routes integrate cleanly because every layer
is gated by require_role and the service contract is small.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import abuse as abuse_module
from app.services import creators as creators_module
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import jobs as jobs_module
from app.services import network as network_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module
from app.services import views as views_module


class FakeWorld:
    def __init__(self):
        self.creators: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, dict[str, Any]] = {}
        self.connections: dict[tuple[str, str], dict[str, Any]] = {}
        self.notifs: list[dict[str, Any]] = []

    def add_creator(self, *, user_id, **kw):
        self.creators[user_id] = {
            "user_id": user_id,
            "full_name": kw.get("full_name", f"Creator {user_id}"),
            "instagram_handle": kw.get("instagram_handle", user_id),
            "niches": kw.get("niches", ["food"]),
            "tier": kw.get("tier", "basic"),
            "follower_range": kw.get("follower_range", "10-50k"),
            "onboarding_completed_at": "2026-05-07T00:00:00Z",
            "engagement_range": None, "creator_tenure": None,
            "neighborhood": None, "primary_platform": "Instagram",
            "content_formats": ["reels"], "hard_limits": [], "bio": None,
        }
        return self.creators[user_id]

    def add_listing(self, *, poster, **kw):
        lid = str(uuid4())
        self.listings[lid] = {
            "id": lid,
            "poster_user_id": poster,
            "title": kw.get("title", "Untitled"),
            "description": kw.get("description", "Body."),
            "listing_type": kw.get("listing_type", "collab"),
            "compensation_text": kw.get("compensation_text"),
            "target_niches": kw.get("target_niches", []),
            "deadline": kw.get("deadline"),
            "is_active": kw.get("is_active", True),
            "is_taken_down": kw.get("is_taken_down", False),
            "taken_down_reason": None,
            "taken_down_by": None, "taken_down_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
        return self.listings[lid]


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creators_by_ids",
        lambda ids: {uid: w.creators[uid] for uid in ids if uid in w.creators},
    )
    monkeypatch.setattr(
        creators_module, "get_for_view", lambda uid: w.creators.get(uid)
    )

    # ----- jobs service -----
    def _list_active(*, niche=None, limit=100):
        rows = [
            lst for lst in w.listings.values()
            if lst["is_active"] and not lst["is_taken_down"]
        ]
        if niche:
            rows = [lst for lst in rows if niche in (lst.get("target_niches") or [])]
        rows.sort(key=lambda lst: lst["created_at"], reverse=True)
        return rows[:limit]

    def _list_by_poster(uid):
        return [lst for lst in w.listings.values() if lst["poster_user_id"] == uid]

    def _list_for_operator(*, taken_down=None):
        rows = list(w.listings.values())
        if taken_down is True:
            rows = [lst for lst in rows if lst["is_taken_down"]]
        elif taken_down is False:
            rows = [lst for lst in rows if not lst["is_taken_down"]]
        return rows

    def _get(lid):
        return w.listings.get(lid)

    def _create(*, poster_id, payload):
        lid = str(uuid4())
        w.listings[lid] = {
            **payload, "id": lid, "poster_user_id": poster_id,
            "is_taken_down": False, "taken_down_reason": None,
            "taken_down_by": None, "taken_down_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
        return lid

    def _update(lid, payload, *, poster_id):
        listing = w.listings.get(lid)
        if listing is None or listing.get("poster_user_id") != poster_id:
            return False
        listing.update(payload)
        return True

    def _deactivate(lid, *, poster_id):
        listing = w.listings.get(lid)
        if not listing or listing["poster_user_id"] != poster_id:
            return False
        listing["is_active"] = False
        return True

    def _take_down(*, listing_id, operator_id, reason):
        if not reason:
            return False
        listing = w.listings.get(listing_id)
        if not listing:
            return False
        listing.update({
            "is_taken_down": True, "taken_down_reason": reason,
            "taken_down_by": operator_id, "taken_down_at": "2026-05-07T00:00:01Z",
            "is_active": False,
        })
        return True

    monkeypatch.setattr(jobs_module, "list_active", _list_active)
    monkeypatch.setattr(jobs_module, "list_by_poster", _list_by_poster)
    monkeypatch.setattr(jobs_module, "list_for_operator", _list_for_operator)
    monkeypatch.setattr(jobs_module, "get", _get)
    monkeypatch.setattr(jobs_module, "create", _create)
    monkeypatch.setattr(jobs_module, "update", _update)
    monkeypatch.setattr(jobs_module, "deactivate", _deactivate)
    monkeypatch.setattr(jobs_module, "take_down", _take_down)

    # ----- network: connection lookup for "can DM" gate -----
    def _get_connection_between(a, b):
        return w.connections.get((min(a, b), max(a, b)))

    monkeypatch.setattr(network_module, "get_connection_between", _get_connection_between)

    # ----- notifications + dms + others quiet -----
    def _create_notif(*, user_id, kind, title, body=None, link_path=None):
        if kind not in notifications_module.KINDS:
            return False
        w.notifs.append({
            "user_id": user_id, "kind": kind, "title": title,
            "body": body, "link_path": link_path,
        })
        return True

    monkeypatch.setattr(notifications_module, "create", _create_notif)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "feedback_for_user", lambda uid, ids: {})
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)
    monkeypatch.setattr(views_module, "record_view", lambda *, viewer_id, viewed_id: True)

    from app.services import audit as audit_module
    monkeypatch.setattr(audit_module, "record", lambda **kw: True)

    return w


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def _signed_in(client, *, role, user_id):
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


# -----------------------------------------------------------------------------
# Creator-side job CRUD
# -----------------------------------------------------------------------------


def test_creator_jobs_board_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_listing(poster="c-2", title="Need a UGC partner")
    r = client.get("/creator/jobs")
    assert r.status_code == 200
    assert "Need a UGC partner" in r.text


def test_creator_jobs_create(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "title": "Looking for a videographer",
            "description": "Half-day shoot in Wynwood next week.",
            "listing_type": "hiring",
            "compensation_text": "$300 + 1hr usage",
            "target_niches": ["fashion", "food"],
            "is_active": "on",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/creator/jobs/")
    # One listing in the world
    assert len(world.listings) == 1


def test_creator_jobs_create_rejects_missing_title(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={"description": "x", "listing_type": "collab"},
    )
    assert r.status_code == 400
    assert world.listings == {}


def test_creator_jobs_edit_only_owner(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-2", title="Theirs")
    r = client.get(f"/creator/jobs/{listing['id']}/edit")
    assert r.status_code == 403


def test_creator_jobs_detail_dm_gate_when_unconnected(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Other")
    listing = world.add_listing(poster="c-2", title="Open")
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "Connect first" in r.text


def test_creator_jobs_detail_dm_unlocks_when_connected(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-2", title="Open")
    world.connections[("c-1", "c-2")] = {"status": "accepted"}
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "Open DM" in r.text


def test_creator_jobs_close(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Mine")
    r = client.post(f"/creator/jobs/{listing['id']}/close")
    assert r.status_code == 303
    assert world.listings[listing["id"]]["is_active"] is False


def test_creator_jobs_404_taken_down(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", is_taken_down=True)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 404


def test_creator_jobs_closed_listing_404_for_non_owner(client, world):
    """Other creators shouldn't be able to read soft-closed listings
    by guessing the UUID (AUDIT.md M4)."""
    _signed_in(client, role="creator", user_id="c-2")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-1", is_active=False)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 404


def test_creator_jobs_closed_listing_visible_to_owner(client, world):
    """Posters still see their own closed listings so they can re-open."""
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", is_active=False)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200


# -----------------------------------------------------------------------------
# Operator: take down
# -----------------------------------------------------------------------------


def test_operator_jobs_list_renders(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_creator(user_id="c-1", full_name="Anna")
    world.add_listing(poster="c-1", title="Open one")
    r = client.get("/operator/jobs")
    assert r.status_code == 200
    assert "Open one" in r.text


def test_operator_takedown_requires_reason(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1")
    r = client.post(f"/operator/jobs/{listing['id']}/takedown", data={"reason": ""})
    assert r.status_code == 400
    assert world.listings[listing["id"]]["is_taken_down"] is False


def test_operator_takedown_succeeds_and_notifies(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1")
    r = client.post(
        f"/operator/jobs/{listing['id']}/takedown",
        data={"reason": "Misleading comp claim."},
    )
    assert r.status_code == 303
    assert world.listings[listing["id"]]["is_taken_down"] is True
    assert any(
        n["user_id"] == "c-1" and n["kind"] == "flag_update"
        for n in world.notifs
    )


def test_operator_jobs_requires_operator(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/operator/jobs")
    assert r.status_code == 403
