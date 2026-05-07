"""Brand verification + brand discovery + outreach + notifications tests.

We stub three services (brands, creators, notifications) and the intel
service so /creator dashboard renders without hitting Supabase. Covers:

  * Operator queue: pending list, detail render, verify (with notification
    fan-out), reject (notes required, also fans out)
  * Brand console: pre-verification pending pane, post-verification
    discovery list (matching by niche overlap + size band)
  * Brand → creator: detail render, outreach validation (min length),
    successful outreach creates a notification
  * Creator notifications: dashboard strip, full list, mark-read, mark-all
  * Creator brand-view: 404 for unverified, render for verified
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import brands as brands_module
from app.services import creators as creators_module
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.brands: dict[str, dict[str, Any]] = {}              # user_id -> brand
        self.creators: dict[str, dict[str, Any]] = {}            # user_id -> creator
        self.notifications: dict[str, dict[str, Any]] = {}        # id -> notification
        self.last_notif_payload: dict[str, Any] | None = None

    def add_brand(self, *, user_id: str, **kwargs) -> dict[str, Any]:
        defaults = {
            "user_id": user_id,
            "company_name": kwargs.get("company_name", "Acme"),
            "brand_website": kwargs.get("brand_website", "https://acme.example"),
            "industry": kwargs.get("industry", "fashion"),
            "is_verified": kwargs.get("is_verified", False),
            "verified_at": kwargs.get("verified_at"),
            "verification_notes": kwargs.get("verification_notes"),
            "onboarding_completed_at": kwargs.get(
                "onboarding_completed_at", "2026-05-07T00:00:00Z"
            ),
            "niche_preferences": kwargs.get("niche_preferences", []),
            "creator_size_preferences": kwargs.get("creator_size_preferences", []),
            "campaign_types": kwargs.get("campaign_types", []),
            "contact_full_name": kwargs.get("contact_full_name", "Sam"),
            "contact_title": kwargs.get("contact_title"),
            "product_description": kwargs.get("product_description"),
            "scale_descriptor": kwargs.get("scale_descriptor"),
            "model_descriptor": kwargs.get("model_descriptor"),
            "positioning_descriptor": kwargs.get("positioning_descriptor"),
            "budget_range": kwargs.get("budget_range"),
        }
        self.brands[user_id] = defaults
        return defaults

    def add_creator(self, *, user_id: str, **kwargs) -> dict[str, Any]:
        defaults = {
            "user_id": user_id,
            "full_name": kwargs.get("full_name", "Anna"),
            "instagram_handle": kwargs.get("instagram_handle", "anna"),
            "niches": kwargs.get("niches", ["food"]),
            "follower_range": kwargs.get("follower_range", "10-50k"),
            "engagement_range": kwargs.get("engagement_range"),
            "creator_tenure": kwargs.get("creator_tenure"),
            "neighborhood": kwargs.get("neighborhood"),
            "primary_platform": kwargs.get("primary_platform", "Instagram"),
            "content_formats": kwargs.get("content_formats", ["reels"]),
            "hard_limits": kwargs.get("hard_limits", []),
            "bio": kwargs.get("bio"),
            "tier": kwargs.get("tier", "basic"),
            "onboarding_completed_at": kwargs.get(
                "onboarding_completed_at", "2026-05-07T00:00:00Z"
            ),
        }
        self.creators[user_id] = defaults
        return defaults


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    # ----- brands service -----
    def _list_pending():
        return [
            b for b in w.brands.values()
            if not b["is_verified"]
            and not b.get("verification_notes")
            and b.get("onboarding_completed_at")
        ]

    def _list_verified():
        return [b for b in w.brands.values() if b["is_verified"]]

    def _list_rejected():
        return [
            b for b in w.brands.values()
            if not b["is_verified"] and b.get("verification_notes")
        ]

    def _get_brand(uid):
        return w.brands.get(uid)

    def _verify(uid, notes):
        b = w.brands.get(uid)
        if not b:
            return False
        b["is_verified"] = True
        b["verified_at"] = "2026-05-07T00:00:00Z"
        b["verification_notes"] = notes or None
        return True

    def _reject(uid, notes):
        if not notes:
            return False
        b = w.brands.get(uid)
        if not b:
            return False
        b["is_verified"] = False
        b["verified_at"] = None
        b["verification_notes"] = notes
        return True

    monkeypatch.setattr(brands_module, "list_pending", _list_pending)
    monkeypatch.setattr(brands_module, "list_verified", _list_verified)
    monkeypatch.setattr(brands_module, "list_rejected", _list_rejected)
    monkeypatch.setattr(brands_module, "get_by_user_id", _get_brand)
    monkeypatch.setattr(brands_module, "verify", _verify)
    monkeypatch.setattr(brands_module, "reject", _reject)

    # ----- creators service -----
    def _list_for_brand_match(*, niche_preferences, creator_size_preferences):
        from app.services.creators import FOLLOWER_BAND_TO_SIZE
        out = []
        bn = set(niche_preferences or [])
        bs = set(creator_size_preferences or [])
        for c in w.creators.values():
            if not c.get("onboarding_completed_at"):
                continue
            cn = set(c.get("niches") or [])
            if bn and not cn.intersection(bn):
                continue
            cs = FOLLOWER_BAND_TO_SIZE.get(c.get("follower_range"))
            if bs and (cs is None or cs not in bs):
                continue
            out.append(c)
        return out

    def _get_creator(uid):
        return w.creators.get(uid)

    monkeypatch.setattr(creators_module, "list_for_brand_match", _list_for_brand_match)
    monkeypatch.setattr(creators_module, "get_for_view", _get_creator)

    # ----- notifications service -----
    def _create(*, user_id, kind, title, body=None, link_path=None):
        if kind not in notifications_module.KINDS:
            return False
        nid = str(uuid4())
        w.notifications[nid] = {
            "id": nid, "user_id": user_id, "kind": kind, "title": title,
            "body": body, "link_path": link_path, "is_read": False,
            "read_at": None, "created_at": "2026-05-07T00:00:00Z",
        }
        w.last_notif_payload = w.notifications[nid]
        return True

    def _list_for_user(uid, *, limit=50):
        rows = [n for n in w.notifications.values() if n["user_id"] == uid]
        rows.sort(key=lambda n: n["created_at"], reverse=True)
        return rows[:limit]

    def _list_unread(uid, *, limit=10):
        rows = [n for n in w.notifications.values()
                if n["user_id"] == uid and not n["is_read"]]
        rows.sort(key=lambda n: n["created_at"], reverse=True)
        return rows[:limit]

    def _unread_count(uid):
        return sum(1 for n in w.notifications.values()
                   if n["user_id"] == uid and not n["is_read"])

    def _mark_read(*, user_id, notification_id):
        n = w.notifications.get(notification_id)
        if n and n["user_id"] == user_id:
            n["is_read"] = True
            n["read_at"] = "2026-05-07T00:00:00Z"
            return True
        return False

    def _mark_all_read(uid):
        for n in w.notifications.values():
            if n["user_id"] == uid:
                n["is_read"] = True
                n["read_at"] = "2026-05-07T00:00:00Z"
        return True

    monkeypatch.setattr(notifications_module, "create", _create)
    monkeypatch.setattr(notifications_module, "list_for_user", _list_for_user)
    monkeypatch.setattr(notifications_module, "list_unread", _list_unread)
    monkeypatch.setattr(notifications_module, "unread_count", _unread_count)
    monkeypatch.setattr(notifications_module, "mark_read", _mark_read)
    monkeypatch.setattr(notifications_module, "mark_all_read", _mark_all_read)

    # ----- profiles service (creator dashboard needs this) -----
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )

    # ----- intel service (creator dashboard calls feed_for_creator) -----
    monkeypatch.setattr(
        intel_module, "feed_for_creator",
        lambda *, niches, tier, category=None: [],
    )
    monkeypatch.setattr(
        intel_module, "feedback_for_user", lambda uid, post_ids: {}
    )

    # ----- operator console calls intel.list_for_operator for status counts -----
    monkeypatch.setattr(intel_module, "list_for_operator", lambda **kw: [])

    # ----- dms (Step 7+: outreach goes through threads/messages) -----
    # We stub these to no-ops; tests/test_dms.py covers the threading
    # contract. Here we just need outreach not to crash.
    threads_state: dict[str, dict[str, Any]] = {}

    def _get_or_create(a, b):
        if a == b:
            return None
        ax, bx = (a, b) if a < b else (b, a)
        key = (ax, bx)
        if key not in threads_state:
            threads_state[key] = {
                "id": str(uuid4()),
                "participant_a_id": ax, "participant_b_id": bx,
                "last_message_at": None, "created_at": "2026-05-07T00:00:00Z",
            }
        return threads_state[key]

    def _get_thread_between(a, b):
        ax, bx = (a, b) if a < b else (b, a)
        return threads_state.get((ax, bx))

    monkeypatch.setattr(dms_module, "get_or_create_thread", _get_or_create)
    monkeypatch.setattr(dms_module, "get_thread_between", _get_thread_between)
    monkeypatch.setattr(
        dms_module, "send_message",
        lambda *, thread_id, sender_id, body: {
            "id": str(uuid4()), "thread_id": thread_id,
            "sender_id": sender_id, "body": body, "read_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        },
    )
    monkeypatch.setattr(dms_module, "list_threads_for_user", lambda uid: [])
    monkeypatch.setattr(dms_module, "list_messages", lambda tid, *, limit=200: [])
    monkeypatch.setattr(
        dms_module, "mark_thread_read_for", lambda tid, *, reader_id: 0
    )
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)

    return w


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str, user_id: str) -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


# -----------------------------------------------------------------------------
# Operator: brand verification queue
# -----------------------------------------------------------------------------


def test_operator_brands_lists_pending(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_brand(user_id="b-1", company_name="Pending Co")
    world.add_brand(user_id="b-2", company_name="Verified Co", is_verified=True)
    world.add_brand(user_id="b-3", company_name="Rejected Co",
                    verification_notes="Site 404")

    r = client.get("/operator/brands")
    assert r.status_code == 200
    assert "Pending Co" in r.text
    assert "Verified Co" not in r.text
    assert "Rejected Co" not in r.text


def test_operator_brands_verified_tab(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_brand(user_id="b-1", company_name="Pending Co")
    world.add_brand(user_id="b-2", company_name="Verified Co", is_verified=True)
    r = client.get("/operator/brands?tab=verified")
    assert "Verified Co" in r.text
    assert "Pending Co" not in r.text


def test_operator_brand_detail_renders(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_brand(user_id="b-1", company_name="Atelier Fig",
                    industry="fashion", niche_preferences=["fashion"])
    r = client.get("/operator/brands/b-1")
    assert r.status_code == 200
    assert "Atelier Fig" in r.text
    assert "fashion" in r.text


def test_operator_brand_detail_404(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/operator/brands/missing")
    assert r.status_code == 404


def test_operator_verify_succeeds_and_notifies(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_brand(user_id="b-1", company_name="Atelier Fig")
    r = client.post(
        "/operator/brands/b-1/verify", data={"notes": "Looks legit."}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/operator/brands"
    assert world.brands["b-1"]["is_verified"] is True
    # Notification was sent to the brand
    assert world.last_notif_payload is not None
    assert world.last_notif_payload["user_id"] == "b-1"
    assert world.last_notif_payload["kind"] == "system"


def test_operator_reject_requires_notes(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_brand(user_id="b-1")
    r = client.post("/operator/brands/b-1/reject", data={"notes": ""})
    assert r.status_code == 400
    assert "required" in r.text.lower()
    assert world.brands["b-1"]["is_verified"] is False
    assert not world.brands["b-1"]["verification_notes"]


def test_operator_reject_with_notes_succeeds_and_notifies(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_brand(user_id="b-1")
    r = client.post(
        "/operator/brands/b-1/reject",
        data={"notes": "Website returns 404. Resubmit when fixed."},
    )
    assert r.status_code == 303
    assert world.brands["b-1"]["verification_notes"].startswith("Website")
    assert world.last_notif_payload["user_id"] == "b-1"
    assert "404" in world.last_notif_payload["body"]


def test_operator_brands_requires_operator_role(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/operator/brands")
    assert r.status_code == 403


# -----------------------------------------------------------------------------
# Brand console
# -----------------------------------------------------------------------------


def test_brand_console_redirects_when_not_onboarded(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", onboarding_completed_at=None)
    r = client.get("/brand")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/brand"


def test_brand_console_shows_pending_pane_when_unverified(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", company_name="Pending Co", is_verified=False)
    r = client.get("/brand")
    assert r.status_code == 200
    assert "Verification pending" in r.text
    assert "Pending Co" in r.text


def test_brand_console_shows_rejection_notes_when_rejected(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", is_verified=False,
                    verification_notes="Site 404")
    r = client.get("/brand")
    assert "Site 404" in r.text


def test_brand_console_lists_matched_creators_when_verified(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(
        user_id="b-1", is_verified=True,
        niche_preferences=["fashion"],
        creator_size_preferences=["micro"],
    )
    world.add_creator(user_id="c-1", full_name="Anna Reyes",
                      niches=["fashion"], follower_range="10-50k")
    world.add_creator(user_id="c-2", full_name="Beauty Person",
                      niches=["beauty"], follower_range="10-50k")
    world.add_creator(user_id="c-3", full_name="Macro Anna",
                      niches=["fashion"], follower_range="500k+")

    r = client.get("/brand")
    assert r.status_code == 200
    assert "Anna Reyes" in r.text
    assert "Beauty Person" not in r.text          # niche mismatch
    assert "Macro Anna" not in r.text             # size mismatch


def test_brand_console_empty_no_match_renders(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(
        user_id="b-1", is_verified=True,
        niche_preferences=["fashion"], creator_size_preferences=["macro"],
    )
    r = client.get("/brand")
    assert "No matches yet" in r.text


# -----------------------------------------------------------------------------
# Brand → creator outreach
# -----------------------------------------------------------------------------


def test_brand_creator_detail_renders_for_verified_brand(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", is_verified=True)
    world.add_creator(user_id="c-1", full_name="Anna Reyes")
    r = client.get("/brand/creators/c-1")
    assert r.status_code == 200
    assert "Anna Reyes" in r.text
    assert "Send outreach" in r.text


def test_brand_creator_detail_404_for_missing_creator(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", is_verified=True)
    r = client.get("/brand/creators/missing")
    assert r.status_code == 404


def test_unverified_brand_cant_view_creator(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", is_verified=False)
    world.add_creator(user_id="c-1")
    r = client.get("/brand/creators/c-1")
    assert r.status_code == 302
    assert r.headers["location"] == "/brand"


def test_outreach_creates_notification(client, world):
    # Step 7: outreach now also creates a thread + first message and
    # links the notification to the DM thread (not the brand profile).
    # Detailed thread/message assertions live in tests/test_dms.py;
    # here we just check the notification fan-out shape.
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", company_name="Atelier Fig", is_verified=True)
    world.add_creator(user_id="c-1", full_name="Anna Reyes")

    r = client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "We'd love to send you our SS26 capsule for a Reel."},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/dm/c-1"
    assert world.last_notif_payload is not None
    n = world.last_notif_payload
    assert n["user_id"] == "c-1"
    assert n["kind"] == "collab_match"
    assert "Atelier Fig" in n["title"]
    assert n["link_path"] == "/creator/dm/b-1"


def test_outreach_rejects_short_pitch(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", is_verified=True)
    world.add_creator(user_id="c-1")
    r = client.post("/brand/creators/c-1/outreach", data={"pitch": "hi"})
    assert r.status_code == 400
    assert world.last_notif_payload is None


# -----------------------------------------------------------------------------
# Creator notifications + brand view
# -----------------------------------------------------------------------------


def test_creator_dashboard_shows_unread_strip(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1", full_name="Anna")
    # Add an unread notification by hand:
    nid = str(uuid4())
    world.notifications[nid] = {
        "id": nid, "user_id": "c-1", "kind": "collab_match",
        "title": "Atelier Fig wants to work with you.",
        "body": "Quick pitch text.",
        "link_path": "/creator/brands/b-1",
        "is_read": False, "read_at": None,
        "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/creator")
    assert r.status_code == 200
    assert "1 new notification" in r.text
    assert "Atelier Fig wants to work with you" in r.text


def test_creator_notifications_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    nid = str(uuid4())
    world.notifications[nid] = {
        "id": nid, "user_id": "c-1", "kind": "system",
        "title": "Welcome.", "body": None,
        "link_path": None, "is_read": False, "read_at": None,
        "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/creator/notifications")
    assert r.status_code == 200
    assert "Welcome." in r.text


def test_creator_mark_read_marks_and_redirects_to_target(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    nid = str(uuid4())
    world.notifications[nid] = {
        "id": nid, "user_id": "c-1", "kind": "collab_match",
        "title": "Pitch", "body": "x", "link_path": "/creator/brands/b-1",
        "is_read": False, "read_at": None,
        "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(
        f"/creator/notifications/{nid}/read",
        data={"target": "/creator/brands/b-1"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/brands/b-1"
    assert world.notifications[nid]["is_read"] is True


def test_creator_mark_all_read(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    for _ in range(3):
        nid = str(uuid4())
        world.notifications[nid] = {
            "id": nid, "user_id": "c-1", "kind": "system",
            "title": "x", "body": None, "link_path": None,
            "is_read": False, "read_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
    r = client.post("/creator/notifications/read-all")
    assert r.status_code == 303
    assert all(n["is_read"] for n in world.notifications.values())


def test_creator_brand_view_404_for_unverified(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_brand(user_id="b-1", is_verified=False)
    r = client.get("/creator/brands/b-1")
    assert r.status_code == 404


def test_creator_brand_view_renders_for_verified(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_brand(
        user_id="b-1", is_verified=True, company_name="Atelier Fig",
        niche_preferences=["fashion"],
    )
    r = client.get("/creator/brands/b-1")
    assert r.status_code == 200
    assert "Atelier Fig" in r.text
