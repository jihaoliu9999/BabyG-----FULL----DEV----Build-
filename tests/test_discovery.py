"""Discovery stack + swipe route tests.

Schema is intentionally not exercised here — it's verified end-to-end
in the swipe-route tests with the Supabase client mocked. These tests
hit the service surface and the route handlers via TestClient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discovery as discovery_module
from app.services import jobs as jobs_module
from app.services import network as network_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module
from app.services import views as views_module

# -----------------------------------------------------------------------------
# In-memory fakes — mirror the layout in test_network.py so tests stay readable.
# -----------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.creators: dict[str, dict[str, Any]] = {}
        self.actions: list[dict[str, Any]] = []
        self.connections: dict[str, dict[str, Any]] = {}

    def add_creator(self, *, user_id: str, **kwargs: Any) -> dict[str, Any]:
        self.creators[user_id] = {
            "user_id": user_id,
            "full_name": kwargs.get("full_name", f"Creator {user_id}"),
            "instagram_handle": kwargs.get("instagram_handle", user_id),
            "niches": kwargs.get("niches", ["food"]),
            "content_formats": kwargs.get("content_formats", ["reels"]),
            "bio": kwargs.get("bio"),
            "location_city": kwargs.get("location_city"),
            "location_region": kwargs.get("location_region"),
            "follower_range": kwargs.get("follower_range", "10-50k"),
            "engagement_range": kwargs.get("engagement_range"),
            "creator_tenure": kwargs.get("creator_tenure"),
            "primary_platform": kwargs.get("primary_platform", "Instagram"),
            "hard_limits": kwargs.get("hard_limits", []),
            "onboarding_completed_at": kwargs.get(
                "onboarding_completed_at", "2026-06-01T00:00:00Z"
            ),
            "profile_photo_url": kwargs.get("profile_photo_url"),
            "updated_at": kwargs.get("updated_at"),
        }
        return self.creators[user_id]

    def add_action(
        self,
        *,
        user_id: str,
        target: str,
        action: str,
        created_at: str | None = None,
    ) -> None:
        self.actions.append(
            {
                "id": str(uuid4()),
                "user_id": user_id,
                "target_user_id": target,
                "action_type": action,
                "created_at": created_at or datetime.now(UTC).isoformat(),
            }
        )

    def add_connection(
        self,
        *,
        requester: str,
        addressee: str,
        status: str = "pending",
    ) -> dict[str, Any]:
        cid = str(uuid4())
        self.connections[cid] = {
            "id": cid,
            "requester_id": requester,
            "addressee_id": addressee,
            "status": status,
        }
        return self.connections[cid]


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()
    monkeypatch.setattr(jobs_module, "list_active", lambda **kw: [])
    monkeypatch.setattr(jobs_module, "get", lambda listing_id: None)

    # Onboarded creators feed.
    def _list_onboarded():
        return [
            row for row in w.creators.values()
            if row.get("onboarding_completed_at")
        ]

    monkeypatch.setattr(network_module, "_list_onboarded_creators", _list_onboarded)

    # Blocked-by-either-side set. For tests we treat any 'blocked' row
    # the same way the real query does — return the peer ids.
    def _blocked_ids(user_id: str) -> set[str]:
        out: set[str] = set()
        for row in w.connections.values():
            if row["status"] != "blocked":
                continue
            if row["requester_id"] == user_id:
                out.add(row["addressee_id"])
            elif row["addressee_id"] == user_id:
                out.add(row["requester_id"])
        return out

    monkeypatch.setattr(network_module, "_blocked_user_ids", _blocked_ids)

    # discovery's own service-client queries — fake them to read from
    # the world. These are the three private helpers in discovery.py.
    def _connected_or_pending_peer_ids(user_id: str) -> set[str]:
        out: set[str] = set()
        for row in w.connections.values():
            requester = row["requester_id"]
            addressee = row["addressee_id"]
            status = row["status"]
            if status in ("accepted", "removed"):
                peer = addressee if requester == user_id else requester
                if peer:
                    out.add(peer)
            elif status == "pending":
                if requester == user_id and addressee:
                    out.add(addressee)
        return out

    monkeypatch.setattr(
        discovery_module,
        "_connected_or_pending_peer_ids",
        _connected_or_pending_peer_ids,
    )

    def _recently_passed_target_ids(user_id: str) -> set[str]:
        cutoff = datetime.now(UTC) - timedelta(
            days=discovery_module.PASSED_COOLDOWN_DAYS
        )
        latest_passed: dict[str, str] = {}
        latest_undo: dict[str, str] = {}
        for row in w.actions:
            if row["user_id"] != user_id:
                continue
            ts_raw = row["created_at"]
            ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            tgt = row["target_user_id"]
            if (
                row["action_type"] == "passed"
                and ts >= cutoff
                and ts_raw > latest_passed.get(tgt, "")
            ):
                latest_passed[tgt] = ts_raw
            elif (
                row["action_type"] == "undo_pass"
                and ts_raw > latest_undo.get(tgt, "")
            ):
                latest_undo[tgt] = ts_raw
        return {
            t for t, p in latest_passed.items() if p > latest_undo.get(t, "")
        }

    monkeypatch.setattr(
        discovery_module,
        "_recently_passed_target_ids",
        _recently_passed_target_ids,
    )

    def _last_undoable_pass(user_id: str) -> str | None:
        rows = sorted(
            (r for r in w.actions if r["user_id"] == user_id
             and r["action_type"] in ("passed", "undo_pass")),
            key=lambda r: r["created_at"],
            reverse=True,
        )
        seen: set[str] = set()
        for r in rows:
            t = r["target_user_id"]
            if t in seen:
                continue
            seen.add(t)
            if r["action_type"] == "passed":
                return t
        return None

    monkeypatch.setattr(
        discovery_module, "last_undoable_pass", _last_undoable_pass
    )

    def _committed_target_ids(user_id: str) -> set[str]:
        out: set[str] = set()
        for row in w.actions:
            if row["user_id"] != user_id:
                continue
            if row["action_type"] in ("connected", "opened_profile"):
                out.add(row["target_user_id"])
        return out

    monkeypatch.setattr(
        discovery_module, "_committed_target_ids", _committed_target_ids
    )

    # record_action goes straight into the action list.
    def _record_action(*, user_id, target_user_id, action_type):
        if action_type not in discovery_module.ALLOWED_ACTIONS:
            return False
        if user_id == target_user_id:
            return False
        w.add_action(user_id=user_id, target=target_user_id, action=action_type)
        return True

    monkeypatch.setattr(discovery_module, "record_action", _record_action)

    # network.list_incoming_pending — keep it a no-op for the route tests.
    monkeypatch.setattr(
        network_module, "list_incoming_pending", lambda uid: []
    )

    # network.request_connection — append a pending connection row.
    def _request_connection(*, requester_id, addressee_id):
        if requester_id == addressee_id:
            return False
        # No duplicate if a non-declined row already exists.
        for row in w.connections.values():
            if (
                row["requester_id"] == requester_id
                and row["addressee_id"] == addressee_id
                and row["status"] in {"pending", "accepted", "blocked"}
            ):
                return False
        w.add_connection(
            requester=requester_id, addressee=addressee_id, status="pending"
        )
        return True

    monkeypatch.setattr(
        network_module, "request_connection", _request_connection
    )

    # Routes use profiles.get_creator_profile to existence-check the peer
    # before recording an action / firing a connection.
    monkeypatch.setattr(
        profiles_module,
        "get_creator_profile",
        lambda uid: w.creators.get(uid),
    )

    # views.record_view + notifications.create — no-ops in tests.
    monkeypatch.setattr(
        views_module, "record_view", lambda viewer_id, viewed_id: True
    )
    monkeypatch.setattr(notifications_module, "create", lambda **kw: True)

    return w


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(c: TestClient, *, role: str = "creator", user_id: str = "c-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    c.cookies.set(SESSION_COOKIE, cookie)


# -----------------------------------------------------------------------------
# Service-level: next_stack_for exclusion rules
# -----------------------------------------------------------------------------


def test_stack_excludes_self_and_blocked(world):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Visible")
    world.add_creator(user_id="c-3", full_name="Blocked")
    world.add_connection(requester="c-1", addressee="c-3", status="blocked")

    stack = discovery_module.next_stack_for("c-1")
    ids = [c["user_id"] for c in stack]
    assert "c-1" not in ids
    assert "c-2" in ids
    assert "c-3" not in ids


def test_stack_excludes_accepted_connection(world):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Already Connected")
    world.add_creator(user_id="c-3", full_name="New")
    world.add_connection(requester="c-1", addressee="c-2", status="accepted")

    stack = discovery_module.next_stack_for("c-1")
    ids = [c["user_id"] for c in stack]
    assert "c-2" not in ids
    assert "c-3" in ids


def test_stack_excludes_outgoing_pending_request(world):
    """The viewer already asked to connect; no need to swipe them again."""
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Pending")
    world.add_connection(requester="c-1", addressee="c-2", status="pending")

    stack = discovery_module.next_stack_for("c-1")
    assert "c-2" not in [c["user_id"] for c in stack]


def test_stack_keeps_incoming_pending_visible(world):
    """If someone else asked to connect with US, they should still
    appear in the swipe so we can react via the full profile view."""
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Awaiting My Response")
    world.add_connection(requester="c-2", addressee="c-1", status="pending")

    stack = discovery_module.next_stack_for("c-1")
    assert "c-2" in [c["user_id"] for c in stack]


def test_stack_excludes_recently_passed_within_cooldown(world):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Recently Passed")
    world.add_creator(user_id="c-3", full_name="New")
    # Yesterday — well inside the 30 day cooldown.
    yesterday = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    world.add_action(user_id="c-1", target="c-2", action="passed", created_at=yesterday)

    stack = discovery_module.next_stack_for("c-1")
    ids = [c["user_id"] for c in stack]
    assert "c-2" not in ids
    assert "c-3" in ids


def test_stack_reincludes_pass_after_cooldown(world):
    """The cooldown is a window, not a permanent block. After it
    expires the creator can reappear."""
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Old Pass")
    # 60 days ago, well past PASSED_COOLDOWN_DAYS (30).
    old = (datetime.now(UTC) - timedelta(days=60)).isoformat()
    world.add_action(user_id="c-1", target="c-2", action="passed", created_at=old)

    stack = discovery_module.next_stack_for("c-1")
    assert "c-2" in [c["user_id"] for c in stack]


def test_stack_permanently_excludes_connected_action(world):
    """`connected` action in discovery is permanent — they're in
    the connections flow now."""
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Connected Long Ago")
    long_ago = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    world.add_action(
        user_id="c-1", target="c-2", action="connected", created_at=long_ago
    )

    stack = discovery_module.next_stack_for("c-1")
    assert "c-2" not in [c["user_id"] for c in stack]


def test_stack_permanently_excludes_opened_profile(world):
    """`opened_profile` is permanent — they've already committed
    attention to that profile."""
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Already Looked At")
    long_ago = (datetime.now(UTC) - timedelta(days=400)).isoformat()
    world.add_action(
        user_id="c-1", target="c-2", action="opened_profile", created_at=long_ago
    )

    stack = discovery_module.next_stack_for("c-1")
    assert "c-2" not in [c["user_id"] for c in stack]


def test_stack_returns_public_projected_rows_only(world):
    world.add_creator(
        user_id="c-1",
    )
    world.add_creator(
        user_id="c-2",
        full_name="Anna",
        profile_photo_url="https://example.test/c2.jpg",
        updated_at="2026-06-16T12:00:00Z",
    )
    # Add a private field that public_creator should strip.
    world.creators["c-2"]["writing_samples"] = "secret draft"
    world.creators["c-2"]["tier"] = "vip"

    stack = discovery_module.next_stack_for("c-1")
    anna = next(c for c in stack if c["user_id"] == "c-2")
    assert anna["profile_photo_url"] == "https://example.test/c2.jpg"
    assert anna["updated_at"] == "2026-06-16T12:00:00Z"
    assert "writing_samples" not in anna
    assert "tier" not in anna


def test_stack_respects_limit(world):
    world.add_creator(user_id="c-1")
    for i in range(2, 10):
        world.add_creator(user_id=f"c-{i}")
    stack = discovery_module.next_stack_for("c-1", limit=3)
    assert len(stack) == 3


def test_stack_empty_when_only_self(world):
    world.add_creator(user_id="c-1")
    assert discovery_module.next_stack_for("c-1") == []


# -----------------------------------------------------------------------------
# Route-level: GET /creator/network swipe page
# -----------------------------------------------------------------------------


def test_swipe_page_renders_top_card_and_records_view(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna")
    world.add_creator(user_id="c-3", full_name="Ben")

    r = client.get("/creator/network")
    assert r.status_code == 200
    # First creator is in the DOM (deterministic order via add_creator
    # insertion + add_action insertion in the fake world).
    assert "Anna" in r.text or "Ben" in r.text
    # One "viewed" action was recorded for the top card.
    viewed = [a for a in world.actions if a["action_type"] == "viewed"]
    assert len(viewed) == 1
    assert viewed[0]["user_id"] == "c-1"


def test_swipe_page_renders_empty_state(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")  # only self

    r = client.get("/creator/network")
    assert r.status_code == 200
    assert "nothing new right now" in r.text
    # No view recorded when there's no card.
    assert all(a["action_type"] != "viewed" for a in world.actions)


# -----------------------------------------------------------------------------
# Route-level: POST /creator/network/swipe
# -----------------------------------------------------------------------------


def test_swipe_pass_records_action_and_redirects_to_stack(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna")

    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-2", "action": "passed"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/network?mode=both"
    passed = [a for a in world.actions if a["action_type"] == "passed"]
    assert len(passed) == 1
    assert passed[0]["target_user_id"] == "c-2"


def test_swipe_connect_fires_request_connection(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna")

    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-2", "action": "connected"},
    )
    assert r.status_code == 303
    # discovery action recorded
    connected_actions = [a for a in world.actions if a["action_type"] == "connected"]
    assert len(connected_actions) == 1
    # connection row created via the existing flow
    pending = [
        c for c in world.connections.values()
        if c["requester_id"] == "c-1" and c["addressee_id"] == "c-2"
    ]
    assert len(pending) == 1
    assert pending[0]["status"] == "pending"


def test_swipe_connect_dedups_when_already_pending(world, client):
    """Re-swiping right on the same creator must not create a second
    connection row. The action is still recorded — that's how the
    swipe stack excludes them next time."""
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-1", addressee="c-2", status="pending")

    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-2", "action": "connected"},
    )
    assert r.status_code == 303
    pending = [
        c for c in world.connections.values()
        if c["requester_id"] == "c-1" and c["addressee_id"] == "c-2"
    ]
    assert len(pending) == 1  # not duplicated


def test_swipe_opened_profile_redirects_to_full_profile(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna")

    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-2", "action": "opened_profile"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/network/c-2"
    opened = [a for a in world.actions if a["action_type"] == "opened_profile"]
    assert len(opened) == 1


def test_swipe_rejects_unknown_action(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-2", "action": "hammer"},
    )
    assert r.status_code == 400


def test_swipe_rejects_self(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-1", "action": "passed"},
    )
    assert r.status_code == 400


def test_swipe_404_for_unknown_target(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-ghost", "action": "passed"},
    )
    assert r.status_code == 404


def test_swipe_requires_creator_role(world, client):
    _signed_in(client, user_id="op-1", role="operator")
    r = client.post(
        "/creator/network/swipe",
        data={"target_user_id": "c-2", "action": "passed"},
    )
    assert r.status_code == 403


# -----------------------------------------------------------------------------
# Profile view records opened_profile (route already exists; new behavior)
# -----------------------------------------------------------------------------


def test_profile_view_records_opened_profile_action(world, client):
    _signed_in(client, user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Anna")

    r = client.get("/creator/network/c-2")
    assert r.status_code == 200
    opened = [a for a in world.actions if a["action_type"] == "opened_profile"]
    assert len(opened) == 1
    assert opened[0]["target_user_id"] == "c-2"


# -----------------------------------------------------------------------------
# Undo pass
# -----------------------------------------------------------------------------


def test_pass_then_undo_restores_creator_to_stack(world):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Second Chance")
    world.add_action(
        user_id="c-1", target="c-2", action="passed",
        created_at="2026-06-01T00:00:00Z",
    )
    # Passed -> excluded.
    assert all(
        c["user_id"] != "c-2" for c in discovery_module.next_stack_for("c-1")
    )
    # Undo (newer than the pass) -> restored.
    world.add_action(
        user_id="c-1", target="c-2", action="undo_pass",
        created_at="2026-06-02T00:00:00Z",
    )
    assert any(
        c["user_id"] == "c-2" for c in discovery_module.next_stack_for("c-1")
    )


def test_last_undoable_pass_picks_most_recent_standing_pass(world):
    world.add_creator(user_id="c-1")
    world.add_action(
        user_id="c-1", target="c-2", action="passed",
        created_at="2026-06-01T00:00:00Z",
    )
    world.add_action(
        user_id="c-1", target="c-3", action="passed",
        created_at="2026-06-02T00:00:00Z",
    )
    assert discovery_module.last_undoable_pass("c-1") == "c-3"
    # Undoing c-3 leaves c-2 as the next undoable pass.
    world.add_action(
        user_id="c-1", target="c-3", action="undo_pass",
        created_at="2026-06-03T00:00:00Z",
    )
    assert discovery_module.last_undoable_pass("c-1") == "c-2"


def test_undo_route_records_action_and_redirects_with_bring_back(world, client):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_action(user_id="c-1", target="c-2", action="passed")
    _signed_in(client, user_id="c-1")

    r = client.post("/creator/network/undo")
    assert r.status_code == 303
    assert "bring_back=c-2" in r.headers["location"]
    assert any(
        a["action_type"] == "undo_pass" and a["target_user_id"] == "c-2"
        for a in world.actions
    )


def test_undo_route_noop_when_nothing_to_undo(world, client):
    world.add_creator(user_id="c-1")
    _signed_in(client, user_id="c-1")
    r = client.post("/creator/network/undo")
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/network"


def test_prioritized_creator_floats_to_top(world):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_creator(user_id="c-3")
    stack = discovery_module.next_stack_for("c-1", prioritize_user_id="c-3")
    assert stack[0]["user_id"] == "c-3"


def test_disconnected_peer_excluded_from_stack(world):
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_connection(requester="c-1", addressee="c-2", status="removed")
    stack = discovery_module.next_stack_for("c-1")
    assert all(c["user_id"] != "c-2" for c in stack)
