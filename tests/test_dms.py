"""DM thread + message tests.

Stubs the dms service plus the surrounding services (brands, creators,
profiles, notifications) so the routes render without hitting Supabase.

Covers:
  * Creator side: thread list, thread render, send message, mark-read on
    open, send fans out a `new_dm` notification, peer must be a verified
    brand
  * Brand side: same shape, mirrored. Peer must be an onboarded creator.
  * Cross-role threading: creator and brand each see the same thread by
    each other's user_id (canonical pair)
  * Outreach now creates a thread + first message + collab_match
    notification linking to the thread (not the brand profile)
  * Role guards (anonymous, wrong role)
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
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.brands: dict[str, dict[str, Any]] = {}
        self.creators: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}        # id -> thread
        self.messages: list[dict[str, Any]] = []
        self.notifications_sent: list[dict[str, Any]] = []

    def add_brand(self, *, user_id, **kwargs):
        b = {
            "user_id": user_id,
            "company_name": kwargs.get("company_name", "Acme"),
            "is_verified": kwargs.get("is_verified", True),
            "onboarding_completed_at": kwargs.get(
                "onboarding_completed_at", "2026-05-07T00:00:00Z"
            ),
            "verification_notes": kwargs.get("verification_notes"),
            "niche_preferences": kwargs.get("niche_preferences", []),
            "creator_size_preferences": kwargs.get("creator_size_preferences", []),
        }
        self.brands[user_id] = b
        return b

    def add_creator(self, *, user_id, **kwargs):
        c = {
            "user_id": user_id,
            "full_name": kwargs.get("full_name", "Anna"),
            "instagram_handle": kwargs.get("instagram_handle", "anna"),
            "niches": kwargs.get("niches", ["food"]),
            "follower_range": kwargs.get("follower_range", "10-50k"),
            "tier": kwargs.get("tier", "basic"),
            "onboarding_completed_at": kwargs.get(
                "onboarding_completed_at", "2026-05-07T00:00:00Z"
            ),
        }
        self.creators[user_id] = c
        return c


def _canonical(a, b):
    return (a, b) if a < b else (b, a)


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    # ----- brands service -----
    monkeypatch.setattr(
        brands_module, "get_by_user_id", lambda uid: w.brands.get(uid)
    )

    # ----- creators service -----
    monkeypatch.setattr(
        creators_module, "get_for_view", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        creators_module, "list_for_brand_match",
        lambda **kw: list(w.creators.values()),
    )

    # ----- profiles (creator dashboard reads it) -----
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )

    # ----- notifications -----
    def _create(*, user_id, kind, title, body=None, link_path=None):
        if kind not in notifications_module.KINDS:
            return False
        w.notifications_sent.append({
            "user_id": user_id, "kind": kind, "title": title,
            "body": body, "link_path": link_path,
        })
        return True

    monkeypatch.setattr(notifications_module, "create", _create)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)

    # ----- dms service -----
    def _get_or_create_thread(a, b):
        if a == b:
            return None
        ax, bx = _canonical(a, b)
        for t in w.threads.values():
            if t["participant_a_id"] == ax and t["participant_b_id"] == bx:
                return t
        t = {
            "id": str(uuid4()),
            "participant_a_id": ax,
            "participant_b_id": bx,
            "last_message_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
        w.threads[t["id"]] = t
        return t

    def _get_thread_between(a, b):
        ax, bx = _canonical(a, b)
        for t in w.threads.values():
            if t["participant_a_id"] == ax and t["participant_b_id"] == bx:
                return t
        return None

    def _list_threads_for_user(uid):
        rows = []
        for t in w.threads.values():
            if uid in (t["participant_a_id"], t["participant_b_id"]):
                tt = dict(t)
                tt["peer_id"] = (
                    tt["participant_b_id"] if tt["participant_a_id"] == uid
                    else tt["participant_a_id"]
                )
                rows.append(tt)
        rows.sort(
            key=lambda r: r["last_message_at"] or "",
            reverse=True,
        )
        return rows

    def _list_messages(thread_id, *, limit=200):
        rows = [m for m in w.messages if m["thread_id"] == thread_id]
        rows.sort(key=lambda m: m["created_at"])
        return rows[:limit]

    def _send_message(*, thread_id, sender_id, body):
        body = (body or "").strip()
        if not body:
            return None
        msg = {
            "id": str(uuid4()),
            "thread_id": thread_id,
            "sender_id": sender_id,
            "body": body[:4000],
            "read_at": None,
            "created_at": f"2026-05-07T00:00:0{len(w.messages) % 10}Z",
        }
        w.messages.append(msg)
        # Bump thread last_message_at
        for t in w.threads.values():
            if t["id"] == thread_id:
                t["last_message_at"] = msg["created_at"]
        return msg

    def _mark_thread_read_for(thread_id, *, reader_id):
        n = 0
        for m in w.messages:
            if (
                m["thread_id"] == thread_id
                and m["sender_id"] != reader_id
                and m["read_at"] is None
            ):
                m["read_at"] = "2026-05-07T00:00:99Z"
                n += 1
        return n

    def _unread_count_for_user(uid):
        thread_ids = {
            t["id"] for t in w.threads.values()
            if uid in (t["participant_a_id"], t["participant_b_id"])
        }
        return sum(
            1 for m in w.messages
            if m["thread_id"] in thread_ids
            and m["sender_id"] != uid
            and m["read_at"] is None
        )

    monkeypatch.setattr(dms_module, "get_or_create_thread", _get_or_create_thread)
    monkeypatch.setattr(dms_module, "get_thread_between", _get_thread_between)
    monkeypatch.setattr(dms_module, "list_threads_for_user", _list_threads_for_user)
    monkeypatch.setattr(dms_module, "list_messages", _list_messages)
    monkeypatch.setattr(dms_module, "send_message", _send_message)
    monkeypatch.setattr(dms_module, "mark_thread_read_for", _mark_thread_read_for)
    monkeypatch.setattr(dms_module, "unread_count_for_user", _unread_count_for_user)

    # ----- intel (creator dashboard) -----
    from app.services import intel as intel_module
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "feedback_for_user", lambda uid, ids: {})

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
# Brand outreach (Step 7 behavior change)
# -----------------------------------------------------------------------------


def test_outreach_creates_thread_message_and_notification(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", company_name="Atelier Fig")
    world.add_creator(user_id="c-1")

    r = client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "We'd love to send you our SS26 capsule for a Reel."},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/dm/c-1"

    # One thread exists with b-1 + c-1 (canonicalized)
    assert len(world.threads) == 1
    t = next(iter(world.threads.values()))
    assert {t["participant_a_id"], t["participant_b_id"]} == {"b-1", "c-1"}

    # First message is the pitch, sent by the brand
    assert len(world.messages) == 1
    m = world.messages[0]
    assert m["sender_id"] == "b-1"
    assert m["body"].startswith("We'd love")

    # Notification fired with the new link target (DM thread, not profile)
    assert len(world.notifications_sent) == 1
    n = world.notifications_sent[0]
    assert n["user_id"] == "c-1"
    assert n["kind"] == "collab_match"
    assert n["link_path"] == "/creator/dm/b-1"


def test_outreach_short_pitch_rejected(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1")
    world.add_creator(user_id="c-1")
    r = client.post("/brand/creators/c-1/outreach", data={"pitch": "hi"})
    assert r.status_code == 400
    assert world.threads == {}
    assert world.messages == []
    assert world.notifications_sent == []


def test_outreach_when_thread_exists_just_appends_message(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1")
    world.add_creator(user_id="c-1")

    client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "First pitch — long enough to pass validation."},
    )
    client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Second pitch — also long enough to pass validation."},
    )
    assert len(world.threads) == 1
    assert len(world.messages) == 2


# -----------------------------------------------------------------------------
# Brand side: DM list + thread
# -----------------------------------------------------------------------------


def test_brand_dm_list_renders(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1")
    world.add_creator(user_id="c-1", full_name="Anna Reyes")
    client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "First pitch — long enough to pass validation."},
    )
    r = client.get("/brand/dm")
    assert r.status_code == 200
    assert "Anna Reyes" in r.text


def test_brand_dm_thread_renders_with_messages(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1")
    world.add_creator(user_id="c-1", full_name="Anna Reyes")
    client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Loving your last reel — collab idea inside."},
    )
    r = client.get("/brand/dm/c-1")
    assert r.status_code == 200
    assert "Loving your last reel" in r.text
    assert "Anna Reyes" in r.text


def test_brand_dm_send_appends_and_notifies(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", company_name="Atelier Fig")
    world.add_creator(user_id="c-1")
    client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Initial pitch — long enough to pass validation."},
    )
    world.notifications_sent.clear()  # ignore the outreach notification

    r = client.post(
        "/brand/dm/c-1/send",
        data={"body": "Confirmed for Tuesday — sending product Monday."},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/dm/c-1"
    assert len(world.messages) == 2
    assert world.messages[-1]["body"].startswith("Confirmed")

    assert len(world.notifications_sent) == 1
    n = world.notifications_sent[0]
    assert n["user_id"] == "c-1"
    assert n["kind"] == "new_dm"
    assert n["link_path"] == "/creator/dm/b-1"


def test_brand_dm_send_empty_body_redirects_without_message(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1")
    world.add_creator(user_id="c-1")
    r = client.post("/brand/dm/c-1/send", data={"body": "   "})
    assert r.status_code == 303
    assert world.messages == []


def test_brand_dm_404_when_creator_missing(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1")
    r = client.get("/brand/dm/missing")
    assert r.status_code == 404


def test_brand_dm_redirects_when_unverified(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    world.add_brand(user_id="b-1", is_verified=False)
    world.add_creator(user_id="c-1")
    r = client.get("/brand/dm/c-1")
    assert r.status_code == 302
    assert r.headers["location"] == "/brand"


# -----------------------------------------------------------------------------
# Creator side: DM list + thread
# -----------------------------------------------------------------------------


def test_creator_dm_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_brand(user_id="b-1", company_name="Atelier Fig")
    # Brand reaches out, creating thread + first message:
    sb_client = TestClient(app, follow_redirects=False)
    _signed_in(sb_client, role="brand", user_id="b-1")
    sb_client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Initial pitch — long enough to pass validation."},
    )

    r = client.get("/creator/dm")
    assert r.status_code == 200
    assert "Atelier Fig" in r.text


def test_creator_dm_thread_marks_messages_read(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_brand(user_id="b-1")
    sb_client = TestClient(app, follow_redirects=False)
    _signed_in(sb_client, role="brand", user_id="b-1")
    sb_client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Initial pitch — long enough to pass validation."},
    )
    # Brand-sent message is unread for the creator
    assert all(m["read_at"] is None for m in world.messages)

    r = client.get("/creator/dm/b-1")
    assert r.status_code == 200
    # On open, the message from the brand becomes read
    assert all(m["read_at"] is not None for m in world.messages)


def test_creator_dm_send_appends_and_notifies(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1", full_name="Anna Reyes",
                      instagram_handle="annareyes")
    world.add_brand(user_id="b-1")
    # Seed thread (brand reached out first)
    sb_client = TestClient(app, follow_redirects=False)
    _signed_in(sb_client, role="brand", user_id="b-1")
    sb_client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Initial pitch — long enough to pass validation."},
    )
    world.notifications_sent.clear()

    r = client.post(
        "/creator/dm/b-1/send",
        data={"body": "Yes, let's chat — Tuesday afternoon works."},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/dm/b-1"
    # 1 outreach message + 1 reply
    assert len(world.messages) == 2
    assert world.messages[-1]["sender_id"] == "c-1"

    assert len(world.notifications_sent) == 1
    n = world.notifications_sent[0]
    assert n["user_id"] == "b-1"
    assert n["kind"] == "new_dm"
    assert n["link_path"] == "/brand/dm/c-1"
    assert "Anna Reyes" in n["title"]


def test_creator_dm_404_for_unverified_brand(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_brand(user_id="b-1", is_verified=False)
    r = client.get("/creator/dm/b-1")
    assert r.status_code == 404


# -----------------------------------------------------------------------------
# Cross-role threading: same thread reachable from both sides
# -----------------------------------------------------------------------------


def test_thread_is_canonical_across_roles(client, world):
    # Brand opens thread first
    world.add_brand(user_id="b-1")
    world.add_creator(user_id="c-1")
    sb_client = TestClient(app, follow_redirects=False)
    _signed_in(sb_client, role="brand", user_id="b-1")
    sb_client.post(
        "/brand/creators/c-1/outreach",
        data={"pitch": "Pitch — long enough to pass validation."},
    )

    # Creator opens the same thread; no second thread is created
    cc_client = TestClient(app, follow_redirects=False)
    _signed_in(cc_client, role="creator", user_id="c-1")
    cc_client.get("/creator/dm/b-1")

    assert len(world.threads) == 1


# -----------------------------------------------------------------------------
# Role guards
# -----------------------------------------------------------------------------


def test_creator_dm_requires_creator_role(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    r = client.get("/creator/dm")
    assert r.status_code == 403


def test_brand_dm_requires_brand_role(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/brand/dm")
    assert r.status_code == 403


def test_dm_routes_require_auth(client, world):
    r = client.get("/creator/dm")
    assert r.status_code == 401
    r = client.get("/brand/dm")
    assert r.status_code == 401
