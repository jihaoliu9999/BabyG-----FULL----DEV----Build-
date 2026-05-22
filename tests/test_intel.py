"""Daily Hot Drops feed + operator intel CRUD tests.

Stubs out the intel service and the profiles service so DB calls become
in-memory dict ops. Covers:

  * Targeting: tier matching, niche overlap, empty target_niches = all,
    expired posts excluded
  * Feedback: signal recorded + reflected as active state on re-render
  * Operator console + intel list/create/edit/archive
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
from app.services import intel as intel_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeIntelStore:
    def __init__(self) -> None:
        self.posts: dict[str, dict[str, Any]] = {}
        self.feedback: dict[tuple[str, str], str] = {}     # (user, post) -> signal
        self.last_create_payload: dict[str, Any] | None = None
        self.last_update_payload: dict[str, Any] | None = None

    def add_post(self, **kwargs) -> dict[str, Any]:
        post = {
            "id": str(uuid4()),
            "title": kwargs.get("title", "Untitled"),
            "body": kwargs.get("body", "Body."),
            "category": kwargs.get("category", "venue"),
            "confidence": kwargs.get("confidence", "medium"),
            "status": kwargs.get("status", "active"),
            "valid_from": kwargs.get("valid_from", "2026-05-01T00:00:00Z"),
            "valid_until": kwargs.get("valid_until", "2099-01-01T00:00:00Z"),
            "target_niches": kwargs.get("target_niches", []),
            "target_tiers": kwargs.get("target_tiers", ["basic", "pro", "vip"]),
            "city": kwargs.get("city", "Miami"),
            "source": kwargs.get("source"),
            "created_at": kwargs.get("created_at", "2026-05-01T00:00:00Z"),
            "created_by": kwargs.get("created_by", "op-1"),
        }
        self.posts[post["id"]] = post
        return post


@pytest.fixture()
def store(monkeypatch) -> FakeIntelStore:
    s = FakeIntelStore()

    def _feed(*, niches, tier, category=None):
        from datetime import UTC, datetime
        now = datetime.now(UTC).isoformat()
        rows = [
            p for p in s.posts.values()
            if p["status"] == "active" and p["valid_until"] > now
        ]
        if category:
            rows = [p for p in rows if p["category"] == category]
        out = []
        creator_niches = set(niches or [])
        for p in rows:
            tt = p.get("target_tiers") or []
            if tt and tier not in tt:
                continue
            tn = p.get("target_niches") or []
            if tn and not creator_niches.intersection(tn):
                continue
            out.append(p)
        out.sort(key=lambda p: p["valid_from"], reverse=True)
        return out

    def _feedback_for_user(uid, post_ids):
        out = {}
        for pid in post_ids:
            sig = s.feedback.get((uid, pid))
            if sig:
                out[pid] = sig
        return out

    def _record_feedback(*, user_id, intel_post_id, signal):
        if signal not in intel_module.FEEDBACK_SIGNALS:
            return False
        s.feedback[(user_id, intel_post_id)] = signal
        return True

    def _list_for_operator(*, status=None, limit=100):
        rows = list(s.posts.values())
        if status:
            rows = [p for p in rows if p["status"] == status]
        rows.sort(key=lambda p: p["created_at"], reverse=True)
        return rows[:limit]

    def _get_intel_post(pid):
        return s.posts.get(pid)

    def _create_intel_post(*, created_by, payload):
        s.last_create_payload = {**payload, "created_by": created_by}
        new = {**payload, "id": str(uuid4()), "created_by": created_by,
               "created_at": "2026-05-07T00:00:00Z"}
        s.posts[new["id"]] = new
        return new["id"]

    def _update_intel_post(pid, payload):
        if pid not in s.posts:
            return False
        s.last_update_payload = payload
        s.posts[pid].update(payload)
        return True

    def _archive_intel_post(pid):
        return _update_intel_post(pid, {"status": "archived"})

    def _status_counts():
        out = {st: 0 for st in intel_module.STATUSES}
        for p in s.posts.values():
            st = p.get("status")
            if st in out:
                out[st] += 1
        return out

    monkeypatch.setattr(intel_module, "feed_for_creator", _feed)
    monkeypatch.setattr(intel_module, "feedback_for_user", _feedback_for_user)
    monkeypatch.setattr(intel_module, "record_feedback", _record_feedback)
    monkeypatch.setattr(intel_module, "list_for_operator", _list_for_operator)
    monkeypatch.setattr(intel_module, "get_intel_post", _get_intel_post)
    monkeypatch.setattr(intel_module, "create_intel_post", _create_intel_post)
    monkeypatch.setattr(intel_module, "update_intel_post", _update_intel_post)
    monkeypatch.setattr(intel_module, "archive_intel_post", _archive_intel_post)
    monkeypatch.setattr(intel_module, "status_counts", _status_counts)

    # Step 6+: creator dashboard reads notifications. Stub empty so the
    # surface still renders in this test file.
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    # Step 7+: creator dashboard reads DM unread count too.
    from app.services import dms as dms_module_local
    monkeypatch.setattr(dms_module_local, "unread_count_for_user", lambda uid: 0)
    # Step 8+: operator console reads abuse pending count.
    from app.services import abuse as abuse_module_local
    monkeypatch.setattr(abuse_module_local, "count_pending", lambda: 0)
    return s


@pytest.fixture()
def fake_creator(monkeypatch):
    """Default: an onboarded creator with niches=[food, fashion], tier=pro."""
    state = {
        "u-1": {
            "user_id": "u-1",
            "full_name": "Anna",
            "niches": ["food", "fashion"],
            "tier": "pro",
            "onboarding_completed_at": "2026-05-07T00:00:00Z",
        }
    }
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: state.get(uid)
    )
    monkeypatch.setattr(
        profiles_module, "is_creator_onboarded",
        lambda uid: bool(state.get(uid, {}).get("onboarding_completed_at")),
    )
    return state


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str, user_id: str = "u-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


# -----------------------------------------------------------------------------
# Targeting (intel.feed_for_creator)
# -----------------------------------------------------------------------------


def test_feed_returns_targeted_active_posts(client, store, fake_creator):
    _signed_in(client, role="creator")
    store.add_post(title="Wynwood opening", target_niches=["food"])
    store.add_post(title="Beauty trend",  target_niches=["beauty"])
    store.add_post(title="Anywhere drop", target_niches=[])    # all niches

    r = client.get("/creator")
    assert r.status_code == 200
    assert "Wynwood opening" in r.text
    assert "Anywhere drop" in r.text
    assert "Beauty trend" not in r.text


def test_feed_excludes_expired_posts(client, store, fake_creator):
    _signed_in(client, role="creator")
    store.add_post(title="Still good", valid_until="2099-01-01T00:00:00Z")
    store.add_post(title="Stale", valid_until="2020-01-01T00:00:00Z")
    r = client.get("/creator")
    assert "Still good" in r.text
    assert "Stale" not in r.text


def test_feed_excludes_non_active_posts(client, store, fake_creator):
    _signed_in(client, role="creator")
    store.add_post(title="Live one")
    store.add_post(title="Drafty", status="draft")
    store.add_post(title="Gone", status="archived")
    r = client.get("/creator")
    assert "Live one" in r.text
    assert "Drafty" not in r.text
    assert "Gone" not in r.text


def test_feed_filters_by_tier(client, store, fake_creator):
    _signed_in(client, role="creator")
    store.add_post(title="VIP only", target_tiers=["vip"])
    store.add_post(title="For all", target_tiers=["basic", "pro", "vip"])
    r = client.get("/creator")
    assert "VIP only" not in r.text       # creator is "pro"
    assert "For all" in r.text


def test_feed_category_filter(client, store, fake_creator):
    _signed_in(client, role="creator")
    store.add_post(title="Venue drop", category="venue")
    store.add_post(title="Trend drop", category="trend")
    r = client.get("/creator?category=venue")
    assert "Venue drop" in r.text
    assert "Trend drop" not in r.text


def test_feed_unknown_category_falls_back_to_all(client, store, fake_creator):
    _signed_in(client, role="creator")
    store.add_post(title="One", category="venue")
    store.add_post(title="Two", category="trend")
    r = client.get("/creator?category=garbage")
    assert "One" in r.text
    assert "Two" in r.text


def test_feed_empty_state_renders(client, store, fake_creator):
    _signed_in(client, role="creator")
    r = client.get("/creator")
    assert r.status_code == 200
    assert "no opportunities yet." in r.text


def test_feed_redirects_to_onboarding_when_creator_not_onboarded(
    client, store, monkeypatch
):
    _signed_in(client, role="creator")
    monkeypatch.setattr(profiles_module, "get_creator_profile", lambda uid: None)
    r = client.get("/creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/creator"


# -----------------------------------------------------------------------------
# Feedback
# -----------------------------------------------------------------------------


def test_feedback_records_and_renders_active(client, store, fake_creator):
    _signed_in(client, role="creator")
    p = store.add_post(title="Try this venue")
    r = client.post(
        f"/creator/intel/{p['id']}/feedback", data={"signal": "useful"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator"
    assert store.feedback[("u-1", p["id"])] == "useful"

    r2 = client.get("/creator")
    assert "feedback-btn-active" in r2.text
    assert 'aria-pressed="true"' in r2.text


def test_feedback_rejects_unknown_signal(client, store, fake_creator):
    _signed_in(client, role="creator")
    p = store.add_post()
    r = client.post(
        f"/creator/intel/{p['id']}/feedback", data={"signal": "garbage"}
    )
    assert r.status_code == 400


def test_feedback_requires_creator_role(client, store):
    _signed_in(client, role="operator")
    r = client.post(
        "/creator/intel/abc/feedback", data={"signal": "useful"}
    )
    assert r.status_code == 403


# -----------------------------------------------------------------------------
# Operator console + intel CRUD
# -----------------------------------------------------------------------------


def test_console_requires_operator(client, store):
    _signed_in(client, role="creator")
    r = client.get("/operator")
    assert r.status_code == 403


def test_console_renders_with_status_counts(client, store):
    _signed_in(client, role="operator")
    store.add_post(status="active")
    store.add_post(status="active")
    store.add_post(status="draft")
    r = client.get("/operator")
    assert r.status_code == 200
    assert "console" in r.text
    # The numbers are rendered around <strong> tags; just check both appear.
    assert ">2</strong> active" in r.text
    assert ">1</strong> draft" in r.text


def test_intel_list_renders_and_filters(client, store):
    _signed_in(client, role="operator")
    store.add_post(title="Alpha", status="active")
    store.add_post(title="Bravo", status="draft")
    r = client.get("/operator/intel")
    assert "Alpha" in r.text
    assert "Bravo" in r.text

    r2 = client.get("/operator/intel?status=draft")
    assert "Alpha" not in r2.text
    assert "Bravo" in r2.text


def test_intel_new_form_renders(client, store):
    _signed_in(client, role="operator")
    r = client.get("/operator/intel/new")
    assert r.status_code == 200
    assert "Publish a Hot Drop" in r.text


def test_intel_create_succeeds(client, store):
    _signed_in(client, role="operator")
    r = client.post(
        "/operator/intel",
        data={
            "title": "Bay Harbor pop-up",
            "body": "Boutique opens Friday; founders willing to gift.",
            "category": "venue",
            "confidence": "high",
            "valid_until": "2099-01-01T00:00",
            "target_niches": ["food", "fashion"],
            "target_tiers": ["basic", "pro", "vip"],
            "city": "Miami",
            "status": "active",
            "source": "https://example.com/source",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/operator/intel"
    p = store.last_create_payload
    assert p["title"] == "Bay Harbor pop-up"
    assert p["category"] == "venue"
    assert p["status"] == "active"
    assert p["target_niches"] == ["food", "fashion"]
    assert p["target_tiers"] == ["basic", "pro", "vip"]


def test_intel_create_rejects_missing_title(client, store):
    _signed_in(client, role="operator")
    r = client.post(
        "/operator/intel",
        data={
            "body": "x", "category": "venue",
            "valid_until": "2099-01-01T00:00",
        },
    )
    assert r.status_code == 400
    assert "title" in r.text.lower()


def test_intel_create_drops_unknown_niches(client, store):
    _signed_in(client, role="operator")
    r = client.post(
        "/operator/intel",
        data={
            "title": "T", "body": "B", "category": "venue",
            "valid_until": "2099-01-01T00:00",
            "target_niches": ["food", "<bad>"],
            "status": "draft",
        },
    )
    assert r.status_code == 303
    assert store.last_create_payload["target_niches"] == ["food"]


def test_intel_edit_renders_existing(client, store):
    _signed_in(client, role="operator")
    p = store.add_post(title="Edit me")
    r = client.get(f"/operator/intel/{p['id']}/edit")
    assert r.status_code == 200
    assert "Edit me" in r.text


def test_intel_update(client, store):
    _signed_in(client, role="operator")
    p = store.add_post(title="Old title")
    r = client.post(
        f"/operator/intel/{p['id']}",
        data={
            "title": "New title", "body": "B", "category": "venue",
            "valid_until": "2099-01-01T00:00", "status": "active",
        },
    )
    assert r.status_code == 303
    assert store.posts[p["id"]]["title"] == "New title"


def test_intel_update_preserves_scheduled_status(client, store):
    # Editing a `scheduled` post must not silently demote it to `draft`.
    _signed_in(client, role="operator")
    p = store.add_post(title="Scheduled post", status="scheduled")
    r = client.post(
        f"/operator/intel/{p['id']}",
        data={
            "title": "Scheduled post", "body": "B", "category": "venue",
            "valid_until": "2099-01-01T00:00", "status": "scheduled",
        },
    )
    assert r.status_code == 303
    assert store.posts[p["id"]]["status"] == "scheduled"


def test_intel_archive(client, store):
    _signed_in(client, role="operator")
    p = store.add_post(title="To archive", status="active")
    r = client.post(f"/operator/intel/{p['id']}/archive")
    assert r.status_code == 303
    assert store.posts[p["id"]]["status"] == "archived"


def test_intel_edit_404_for_unknown(client, store):
    _signed_in(client, role="operator")
    r = client.get("/operator/intel/missing-id/edit")
    assert r.status_code == 404
