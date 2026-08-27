"""DM thread + message tests.

Stubs the dms service plus the surrounding services (profiles,
notifications) so the routes render without hitting Supabase.

Covers:
  * Creator side: thread list, thread render, send message, mark-read
    on open, send fans out a `new_dm` notification, peer must be a
    connected creator
  * Role guards (anonymous, wrong role)
  * dm_preference gate helper — recipient_accepts_cold_thread
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import dm_briefs as dm_briefs_module
from app.services import dms as dms_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.creators: dict[str, dict[str, Any]] = {}
        self.threads: dict[str, dict[str, Any]] = {}        # id -> thread
        self.messages: list[dict[str, Any]] = []
        self.notifications_sent: list[dict[str, Any]] = []

    def add_creator(self, *, user_id, **kwargs):
        c = {
            "user_id": user_id,
            "full_name": kwargs.get("full_name", "Anna"),
            "instagram_handle": kwargs.get("instagram_handle", "anna"),
            "niches": kwargs.get("niches", ["food"]),
            "follower_range": kwargs.get("follower_range", "10-50k"),
            "tier": kwargs.get("tier", "basic"),
            "babyg_auto_brief_dms": kwargs.get("babyg_auto_brief_dms", True),
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

    # ----- profiles (creator dashboard reads it) -----
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creators_by_ids",
        lambda ids: {u: w.creators[u] for u in ids if u in w.creators},
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

    def _list_messages(thread_id, *, participant_id, limit=200):
        t = w.threads.get(thread_id)
        if t is None or participant_id not in (
            t["participant_a_id"], t["participant_b_id"]
        ):
            return []
        rows = [m for m in w.messages if m["thread_id"] == thread_id]
        rows.sort(key=lambda m: m["created_at"])
        return rows[:limit]

    def _list_messages_for_operator(thread_id, *, limit=200):
        rows = [m for m in w.messages if m["thread_id"] == thread_id]
        rows.sort(key=lambda m: m["created_at"])
        return rows[:limit]

    def _send_message(*, thread_id, sender_id, body):
        body = (body or "").strip()
        if not body:
            return None
        t = w.threads.get(thread_id)
        if t is None or sender_id not in (
            t["participant_a_id"], t["participant_b_id"]
        ):
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
    monkeypatch.setattr(
        dms_module, "list_messages_for_operator", _list_messages_for_operator
    )
    monkeypatch.setattr(dms_module, "send_message", _send_message)
    monkeypatch.setattr(dms_module, "mark_thread_read_for", _mark_thread_read_for)
    monkeypatch.setattr(dms_module, "unread_count_for_user", _unread_count_for_user)

    def _unread_counts_by_thread(uid, thread_ids):
        counts: dict[str, int] = {}
        for tid in thread_ids:
            counts[tid] = sum(
                1 for m in w.messages
                if str(m["thread_id"]) == str(tid)
                and m["sender_id"] != uid
                and m["read_at"] is None
            )
        return {k: v for k, v in counts.items() if v}

    def _last_messages_by_thread(thread_ids):
        latest: dict[str, dict] = {}
        for tid in thread_ids:
            same = [m for m in w.messages if str(m["thread_id"]) == str(tid)]
            same.sort(key=lambda m: m.get("created_at", ""), reverse=True)
            if same:
                m = same[0]
                latest[str(tid)] = {
                    "body": m.get("body") or "",
                    "sender_id": str(m.get("sender_id") or ""),
                    "created_at": m.get("created_at"),
                }
        return latest

    monkeypatch.setattr(dms_module, "unread_counts_by_thread", _unread_counts_by_thread)
    monkeypatch.setattr(dms_module, "last_messages_by_thread", _last_messages_by_thread)

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
# Creator side: DM list + thread
#
# v1 is creator-only. Brand outreach + brand DM tests
# (test_outreach_*, test_brand_dm_*) shipped on the brand-side-v1.5
# branch.
# -----------------------------------------------------------------------------


def _seed_thread(world, *, a, b, body, sender):
    """Insert a thread + one message directly so creator DM tests don't
    have to set up an accepted-connection round-trip. Mirrors the
    canonical-pair shape the stubs in `world` use."""
    ax, bx = _canonical(a, b)
    tid = str(uuid4())
    world.threads[tid] = {
        "id": tid,
        "participant_a_id": ax,
        "participant_b_id": bx,
        "last_message_at": "2026-05-07T00:00:00Z",
        "created_at": "2026-05-07T00:00:00Z",
    }
    world.messages.append(
        {
            "id": str(uuid4()),
            "thread_id": tid,
            "sender_id": sender,
            "body": body,
            "read_at": None,
            "created_at": "2026-05-07T00:00:01Z",
        }
    )


def _accepted_connection(monkeypatch, *, a, b):
    """Pretend (a, b) have an accepted creator-creator connection so
    `_resolve_creator_dm_peer` admits the peer."""
    from app.services import network as network_module

    def _between(x, y):
        if {x, y} == {a, b}:
            return {"status": "accepted", "requester_id": a, "addressee_id": b}
        return None

    monkeypatch.setattr(network_module, "get_connection_between", _between)


def test_creator_dm_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna Reyes")
    _seed_thread(world, a="c-2", b="c-1", body="hello", sender="c-2")

    r = client.get("/creator/dm")
    assert r.status_code == 200
    assert "Anna Reyes" in r.text
    assert 'class="dm-inbox-search"' in r.text
    assert "data-dm-search" in r.text
    assert 'placeholder="Search conversations"' in r.text
    assert 'href="/creator/network"' in r.text
    assert " is-dm " in r.text
    assert 'class="app-topbar"' not in r.text
    assert 'class="mobile-header"' not in r.text
    assert "ask babyg to read" not in r.text
    assert 'action="/creator/dm/c-2/brief"' not in r.text


def test_creator_dm_list_search_filters_by_peer(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna Reyes")
    world.add_creator(user_id="c-3", full_name="Maya Chen")
    _seed_thread(world, a="c-2", b="c-1", body="hello", sender="c-2")
    _seed_thread(world, a="c-3", b="c-1", body="hello", sender="c-3")

    r = client.get("/creator/dm?q=maya")

    assert r.status_code == 200
    assert "Maya Chen" in r.text
    assert "Anna Reyes" not in r.text


def test_creator_dm_thread_marks_messages_read(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")
    _seed_thread(world, a="c-2", b="c-1", body="hi from c-2", sender="c-2")
    assert all(m["read_at"] is None for m in world.messages)

    r = client.get("/creator/dm/c-2")
    assert r.status_code == 200
    assert all(m["read_at"] is not None for m in world.messages)
    # The slim thread header carries the "ask babyg" refresh action inside
    # its ⋯ menu now; the form still POSTs to the same brief endpoint.
    assert 'action="/creator/dm/c-2/brief"' in r.text
    assert "ask babyg" in r.text
    assert "ask babyg about this message" not in r.text
    assert "ask babyg to re-check" not in r.text


def test_creator_dm_thread_omits_shared_profile_chrome(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")

    r = client.get("/creator/dm/c-2")

    assert r.status_code == 200
    assert " is-dm " in r.text
    assert " is-dm-thread" in r.text
    assert 'class="app-topbar"' not in r.text
    assert 'class="mobile-header"' not in r.text


def test_creator_dm_send_appends_and_notifies(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(
        user_id="c-1", full_name="Anna Reyes", instagram_handle="annareyes"
    )
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")
    _seed_thread(world, a="c-2", b="c-1", body="hi", sender="c-2")
    world.notifications_sent.clear()

    r = client.post(
        "/creator/dm/c-2/send",
        data={"body": "Yes, let's chat — Tuesday afternoon works."},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/dm/c-2"
    # 1 seeded message + 1 reply
    assert len(world.messages) == 2
    assert world.messages[-1]["sender_id"] == "c-1"

    assert len(world.notifications_sent) == 1
    n = world.notifications_sent[0]
    assert n["user_id"] == "c-2"
    assert n["kind"] == "new_dm"
    assert n["link_path"] == "/creator/dm/c-1"
    assert "Anna Reyes" in n["title"]


def test_auto_brief_preference_is_passed_to_service(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1", babyg_auto_brief_dms=False)
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")
    _seed_thread(
        world, a="c-2", b="c-1", body="what is the budget?", sender="c-2"
    )
    captured: dict[str, Any] = {}

    def _brief(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(dm_briefs_module, "get_or_generate_brief", _brief)
    response = client.get("/creator/dm/c-2")
    assert response.status_code == 200
    assert captured["auto_enabled"] is False
    assert captured["force"] is False
    assert captured["recent_messages"] == world.messages


def test_manual_brief_bypasses_disabled_auto_preference(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1", babyg_auto_brief_dms=False)
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")
    _seed_thread(world, a="c-2", b="c-1", body="thanks", sender="c-2")
    captured: dict[str, Any] = {}

    def _brief(**kwargs):
        captured.update(kwargs)
        return {"risk_level": "unclear"}

    monkeypatch.setattr(dm_briefs_module, "get_or_generate_brief", _brief)
    response = client.post(
        "/creator/dm/c-2/brief", headers={"X-Requested-With": "fetch"}
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert captured["auto_enabled"] is False
    assert captured["force"] is True


def test_manual_brief_refresh_is_rate_limited(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")
    _seed_thread(world, a="c-2", b="c-1", body="budget?", sender="c-2")
    monkeypatch.setattr(
        dm_briefs_module,
        "get_or_generate_brief",
        lambda **kwargs: {"risk_level": "unclear"},
    )
    responses = [
        client.post(
            "/creator/dm/c-2/brief", headers={"X-Requested-With": "fetch"}
        )
        for _ in range(6)
    ]
    assert [response.status_code for response in responses] == [200, 200, 200, 200, 200, 429]


def test_brief_follow_up_is_recipient_scoped_and_ephemeral(
    client, world, monkeypatch
):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    _accepted_connection(monkeypatch, a="c-2", b="c-1")
    _seed_thread(world, a="c-2", b="c-1", body="Usage is forever.", sender="c-2")
    captured: dict[str, Any] = {}

    def _follow_up(**kwargs):
        captured.update(kwargs)
        return {"title": "rights", "analysis": "Ask for a term.", "draft": ""}

    monkeypatch.setattr(dm_briefs_module, "generate_follow_up", _follow_up)
    response = client.post(
        "/creator/dm/c-2/brief/follow-up", data={"focus": "check_rights"}
    )
    assert response.status_code == 200
    assert response.json()["result"]["title"] == "rights"
    assert captured["recipient_id"] == "c-1"
    assert captured["messages"] == world.messages


def test_creator_dm_404_for_unconnected_creator(client, world, monkeypatch):
    """Without an accepted connection, the DM peer isn't reachable
    (gate against cold messaging)."""
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    # No `_accepted_connection` call — connection lookup returns None.
    from app.services import network as network_module
    monkeypatch.setattr(
        network_module, "get_connection_between", lambda x, y: None
    )
    r = client.get("/creator/dm/c-2")
    assert r.status_code == 404


# -----------------------------------------------------------------------------
# Role guards
# -----------------------------------------------------------------------------


def test_creator_dm_requires_creator_role(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/dm")
    assert r.status_code == 403


def test_dm_routes_require_auth(client, world):
    # HTML GETs redirect to login; JSON GETs still return 401.
    r = client.get("/creator/dm")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?role=creator"
    r = client.get("/creator/dm", headers={"accept": "application/json"})
    assert r.status_code == 401


# -----------------------------------------------------------------------------
# Phase 3 — dm_preference gate (helper only; consumer lands in Phase 2)
# -----------------------------------------------------------------------------


def test_recipient_accepts_cold_thread_default_open():
    """Profiles without an explicit dm_preference default to ``open`` —
    matches migration 0016's column default."""
    assert dms_module.recipient_accepts_cold_thread({"user_id": "u1"}) is True
    assert (
        dms_module.recipient_accepts_cold_thread(
            {"user_id": "u1", "dm_preference": "open"}
        )
        is True
    )


def test_recipient_accepts_cold_thread_connections_only_rejects():
    assert (
        dms_module.recipient_accepts_cold_thread(
            {"user_id": "u1", "dm_preference": "connections_only"}
        )
        is False
    )


def test_recipient_accepts_cold_thread_fails_closed_on_missing_profile():
    """No profile = no permission. Defensive — never invent consent
    from absence of data. Real rows always have dm_preference because
    the column is NOT NULL DEFAULT 'open', so {} can only mean a
    lookup miss."""
    assert dms_module.recipient_accepts_cold_thread(None) is False
    assert dms_module.recipient_accepts_cold_thread({}) is False


def test_recipient_accepts_cold_thread_unknown_value_treated_as_open():
    """Garbage in the column shouldn't lock a creator out of their own
    DMs. Anything not equal to 'connections_only' falls through to open."""
    assert (
        dms_module.recipient_accepts_cold_thread(
            {"dm_preference": "shouted_through_a_megaphone"}
        )
        is True
    )
