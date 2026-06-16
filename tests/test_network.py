"""Creator network directory + connection tests.

Covers:
  * Directory: lists onboarded creators except self and blocked relationships
  * Profile view: 404 for missing/un-onboarded; 404 for blocked; CTA state
    differs by connection status (none / outgoing / incoming / connected)
  * Request: validates self-target, missing peer, hits notification fan-out
  * Connections list shows accepted / incoming / outgoing tabs correctly
  * Respond: only the addressee accepts/declines; either side can block;
    unknown action 400
  * Role guards
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
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import network as network_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module
from app.services import views as views_module


class FakeWorld:
    def __init__(self) -> None:
        self.creators: dict[str, dict[str, Any]] = {}
        self.connections: dict[str, dict[str, Any]] = {}
        self.notifications_sent: list[dict[str, Any]] = []

    def add_creator(self, *, user_id, **kwargs):
        self.creators[user_id] = {
            "user_id": user_id,
            "full_name": kwargs.get("full_name", f"Creator {user_id}"),
            "instagram_handle": kwargs.get("instagram_handle", user_id),
            "niches": kwargs.get("niches", ["food"]),
            "follower_range": kwargs.get("follower_range", "10-50k"),
            "tier": kwargs.get("tier", "basic"),
            "bio": kwargs.get("bio"),
            "location_city": kwargs.get("location_city"),
            "location_region": kwargs.get("location_region"),
            "location_lat": kwargs.get("location_lat"),
            "location_lng": kwargs.get("location_lng"),
            "primary_platform": kwargs.get("primary_platform", "Instagram"),
            "engagement_range": kwargs.get("engagement_range"),
            "creator_tenure": kwargs.get("creator_tenure"),
            "content_formats": kwargs.get("content_formats", ["reels"]),
            "onboarding_completed_at": kwargs.get(
                "onboarding_completed_at", "2026-05-07T00:00:00Z"
            ),
        }
        return self.creators[user_id]

    def add_connection(self, *, requester, addressee, status="pending"):
        cid = str(uuid4())
        self.connections[cid] = {
            "id": cid,
            "requester_id": requester, "addressee_id": addressee,
            "status": status,
            "requested_at": "2026-05-07T00:00:00Z",
            "responded_at": None if status == "pending" else "2026-05-07T00:00:01Z",
        }
        return self.connections[cid]


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    # ----- profiles -----
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creators_by_ids",
        lambda ids: {uid: w.creators[uid] for uid in ids if uid in w.creators},
    )

    # ----- network service -----
    def _list_directory(uid):
        blocked = set()
        for c in w.connections.values():
            if c["status"] == "blocked" and uid in (c["requester_id"], c["addressee_id"]):
                peer = (
                    c["addressee_id"] if c["requester_id"] == uid
                    else c["requester_id"]
                )
                blocked.add(peer)
        return [
            profiles_module.public_creator(r) for r in w.creators.values()
            if r.get("user_id") != uid
            and r.get("user_id") not in blocked
            and r.get("onboarding_completed_at")
        ]

    def _get_connection_between(a, b):
        for c in w.connections.values():
            pair = {c["requester_id"], c["addressee_id"]}
            if pair == {a, b}:
                return c
        return None

    def _request_connection(*, requester_id, addressee_id):
        if requester_id == addressee_id:
            return False
        existing = _get_connection_between(requester_id, addressee_id)
        if existing:
            if existing["status"] in {"pending", "accepted", "blocked"}:
                return False
            if existing["status"] == "declined":
                existing["requester_id"] = requester_id
                existing["addressee_id"] = addressee_id
                existing["status"] = "pending"
                existing["requested_at"] = "2026-05-08T00:00:00Z"
                existing["responded_at"] = None
                return True
        w.add_connection(
            requester=requester_id, addressee=addressee_id, status="pending"
        )
        return True

    def _respond(*, connection_id, responder_id, action):
        if action not in network_module.ACTION_TO_STATUS:
            return False
        c = w.connections.get(connection_id)
        if c is None:
            return False
        if action in ("accept", "decline") and c["addressee_id"] != responder_id:
            return False
        if action == "block" and responder_id not in (
            c["addressee_id"], c["requester_id"]
        ):
            return False
        c["status"] = network_module.ACTION_TO_STATUS[action]
        c["responded_at"] = "2026-05-07T00:00:01Z"
        return True

    def _list_for(uid, *, status, as_addressee=None):
        rows = [c for c in w.connections.values() if c["status"] == status]
        if as_addressee is True:
            rows = [c for c in rows if c["addressee_id"] == uid]
        elif as_addressee is False:
            rows = [c for c in rows if c["requester_id"] == uid]
        else:
            rows = [
                c for c in rows
                if uid in (c["requester_id"], c["addressee_id"])
            ]
        for r in rows:
            r["peer_id"] = (
                r["addressee_id"] if r["requester_id"] == uid else r["requester_id"]
            )
        return rows

    monkeypatch.setattr(network_module, "list_directory_for_creator", _list_directory)
    monkeypatch.setattr(network_module, "get_connection_between", _get_connection_between)
    monkeypatch.setattr(network_module, "request_connection", _request_connection)
    monkeypatch.setattr(network_module, "respond_to_connection", _respond)
    monkeypatch.setattr(
        network_module, "list_accepted_for_user",
        lambda uid: _list_for(uid, status="accepted"),
    )
    monkeypatch.setattr(
        network_module, "list_incoming_pending",
        lambda uid: _list_for(uid, status="pending", as_addressee=True),
    )
    monkeypatch.setattr(
        network_module, "list_outgoing_pending",
        lambda uid: _list_for(uid, status="pending", as_addressee=False),
    )

    # ----- notifications + dms quiet -----
    def _notif_create(*, user_id, kind, title, body=None, link_path=None):
        if kind not in notifications_module.KINDS:
            return False
        w.notifications_sent.append({
            "user_id": user_id, "kind": kind, "title": title,
            "body": body, "link_path": link_path,
        })
        return True

    monkeypatch.setattr(notifications_module, "create", _notif_create)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)

    # connection accept/decline now records audit; stub it.
    from app.services import audit as audit_module
    monkeypatch.setattr(audit_module, "record", lambda **kw: True)

    # ----- intel + abuse quiet -----
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "feedback_for_user", lambda uid, ids: {})
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)

    # Step 10: profile views recorded on every network/{peer} GET.
    monkeypatch.setattr(
        views_module, "record_view", lambda *, viewer_id, viewed_id: True
    )
    monkeypatch.setattr(views_module, "count_distinct_viewers", lambda uid: 0)
    monkeypatch.setattr(views_module, "list_recent_viewers", lambda uid: [])
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
# Directory
# -----------------------------------------------------------------------------


def test_directory_lists_other_onboarded_creators(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1", full_name="Me Self")
    world.add_creator(user_id="c-2", full_name="Anna Reyes")
    world.add_creator(user_id="c-3", full_name="Ben Lee")

    r = client.get("/creator/network")
    assert r.status_code == 200
    assert "Anna Reyes" in r.text
    assert "Ben Lee" in r.text
    # Self should not appear
    assert "Me Self" not in r.text


def test_directory_excludes_blocked_peers(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Visible")
    world.add_creator(user_id="c-3", full_name="Blocked Person")
    world.add_connection(requester="c-1", addressee="c-3", status="blocked")
    r = client.get("/creator/network")
    assert "Visible" in r.text
    assert "Blocked Person" not in r.text


def test_directory_requires_creator_role(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/network")
    assert r.status_code == 403


# -----------------------------------------------------------------------------
# Profile view
# -----------------------------------------------------------------------------


def test_profile_view_renders_with_connect_cta_when_no_connection(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna Reyes")
    r = client.get("/creator/network/c-2")
    assert r.status_code == 200
    assert "Send connect request" in r.text


def test_profile_view_shows_pending_when_outgoing(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-1", addressee="c-2", status="pending")
    r = client.get("/creator/network/c-2")
    assert "Request sent" in r.text


def test_profile_view_shows_accept_when_incoming(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-2", addressee="c-1", status="pending")
    r = client.get("/creator/network/c-2")
    assert "Accept" in r.text
    assert "Decline" in r.text


def test_profile_view_shows_connected_state(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-1", addressee="c-2", status="accepted")
    r = client.get("/creator/network/c-2")
    assert "You're connected" in r.text


def test_profile_view_404_for_blocked(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-1", addressee="c-2", status="blocked")
    r = client.get("/creator/network/c-2")
    assert r.status_code == 404


def test_profile_view_404_for_missing(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.get("/creator/network/c-missing")
    assert r.status_code == 404


def test_profile_view_self_redirects(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.get("/creator/network/c-1")
    assert r.status_code == 302
    assert r.headers["location"] == "/creator/network"


# -----------------------------------------------------------------------------
# Request connection
# -----------------------------------------------------------------------------


def test_request_creates_pending_and_notifies(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna Reyes")

    r = client.post(
        "/creator/connections/request", data={"peer_user_id": "c-2"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/network/c-2"
    assert any(
        c["requester_id"] == "c-1" and c["addressee_id"] == "c-2"
        and c["status"] == "pending"
        for c in world.connections.values()
    )
    assert any(
        n["user_id"] == "c-2" and n["kind"] == "connection_request"
        for n in world.notifications_sent
    )


def test_request_self_400(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/connections/request", data={"peer_user_id": "c-1"}
    )
    assert r.status_code == 400


def test_request_missing_peer_404(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/connections/request", data={"peer_user_id": "ghost"}
    )
    assert r.status_code == 404


def test_request_after_decline_reuses_row_and_flips_to_pending(client, world):
    # c-1 sent a request, c-2 declined. c-1 sends again — must succeed
    # without colliding on the (requester, addressee) UNIQUE constraint.
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    declined = world.add_connection(
        requester="c-1", addressee="c-2", status="declined"
    )
    r = client.post(
        "/creator/connections/request", data={"peer_user_id": "c-2"}
    )
    assert r.status_code == 303
    assert len(world.connections) == 1                   # same row, not a duplicate
    assert world.connections[declined["id"]]["status"] == "pending"
    assert world.connections[declined["id"]]["responded_at"] is None
    # The addressee should get a connection_request notification.
    assert any(n["kind"] == "connection_request" for n in world.notifications_sent)


def test_request_after_decline_from_other_side_re_orients_row(client, world):
    # c-2 declined c-1's request. Later c-2 has a change of heart and
    # requests c-1. The same row flips: requester becomes c-2.
    _signed_in(client, role="creator", user_id="c-2")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    declined = world.add_connection(
        requester="c-1", addressee="c-2", status="declined"
    )
    r = client.post(
        "/creator/connections/request", data={"peer_user_id": "c-1"}
    )
    assert r.status_code == 303
    assert len(world.connections) == 1
    row = world.connections[declined["id"]]
    assert row["requester_id"] == "c-2"
    assert row["addressee_id"] == "c-1"
    assert row["status"] == "pending"


def test_request_no_op_when_pending_exists(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-1", addressee="c-2", status="pending")
    r = client.post(
        "/creator/connections/request", data={"peer_user_id": "c-2"}
    )
    assert r.status_code == 303                      # idempotent redirect
    assert len(world.connections) == 1               # didn't double-insert
    assert world.notifications_sent == []            # no extra notification


# -----------------------------------------------------------------------------
# Connections page + respond
# -----------------------------------------------------------------------------


def test_connections_page_groups_correctly(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Accepted Friend")
    world.add_creator(user_id="c-3", full_name="Incoming Request")
    world.add_creator(user_id="c-4", full_name="Outgoing Request")
    world.add_connection(requester="c-1", addressee="c-2", status="accepted")
    world.add_connection(requester="c-3", addressee="c-1", status="pending")
    world.add_connection(requester="c-1", addressee="c-4", status="pending")

    r = client.get("/creator/connections")
    assert r.status_code == 200
    text = r.text
    assert "Accepted Friend" in text
    assert "Incoming Request" in text
    assert "Outgoing Request" in text
    # Accept / decline buttons appear in the incoming row
    assert "Accept" in text
    assert "Decline" in text


def test_respond_accept_only_by_addressee(client, world):
    # Requester tries to accept their own request → no-op
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    c = world.add_connection(requester="c-1", addressee="c-2", status="pending")
    r = client.post(f"/creator/connections/{c['id']}/accept")
    assert r.status_code == 303
    assert world.connections[c["id"]]["status"] == "pending"


def test_respond_addressee_accept_succeeds(client, world):
    _signed_in(client, role="creator", user_id="c-2")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    c = world.add_connection(requester="c-1", addressee="c-2", status="pending")
    r = client.post(f"/creator/connections/{c['id']}/accept")
    assert r.status_code == 303
    assert world.connections[c["id"]]["status"] == "accepted"


def test_respond_addressee_decline_succeeds(client, world):
    _signed_in(client, role="creator", user_id="c-2")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    c = world.add_connection(requester="c-1", addressee="c-2", status="pending")
    r = client.post(f"/creator/connections/{c['id']}/decline")
    assert r.status_code == 303
    assert world.connections[c["id"]]["status"] == "declined"


def test_respond_block_either_side(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    c = world.add_connection(requester="c-1", addressee="c-2", status="accepted")
    r = client.post(f"/creator/connections/{c['id']}/block")
    assert r.status_code == 303
    assert world.connections[c["id"]]["status"] == "blocked"


def test_respond_unknown_action_400(client, world):
    _signed_in(client, role="creator", user_id="c-2")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    c = world.add_connection(requester="c-1", addressee="c-2")
    r = client.post(f"/creator/connections/{c['id']}/hammer")
    assert r.status_code == 400


def test_connections_requires_creator_role(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/connections")
    assert r.status_code == 403
