"""Abuse-report submission + operator review tests.

Covers:
  * /report from a creator (DM thread, profile)
  * /report from a brand (DM thread, profile)
  * /report rejects unknown target_type
  * /report enforces same-origin redirect on return_to (open-redirect guard)
  * Operator queue list, status filter
  * Operator detail renders thread / profile preview
  * Operator dismiss (notes optional), action (notes required), escalate
    (notes required) + reporter notification fan-out
  * Operators can't file reports themselves (403)
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
from app.services import brands as brands_module
from app.services import dms as dms_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module

# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeWorld:
    def __init__(self) -> None:
        self.reports: dict[str, dict[str, Any]] = {}        # id -> report
        self.brands: dict[str, dict[str, Any]] = {}
        self.creators: dict[str, dict[str, Any]] = {}
        self.thread_messages: dict[str, list[dict[str, Any]]] = {}
        self.notifications_sent: list[dict[str, Any]] = []
        self.last_report: dict[str, Any] | None = None


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    # ----- abuse service -----
    def _create(*, reporter_id, target_type, target_id, reason):
        if target_type not in abuse_module.TARGET_TYPES:
            return False
        if not (reason or "").strip() or len((reason or "").strip()) < 10:
            return False
        rid = str(uuid4())
        row = {
            "id": rid, "reporter_id": reporter_id,
            "target_type": target_type, "target_id": target_id,
            "reason": reason.strip(), "status": "pending",
            "reviewed_by": None, "reviewed_at": None, "action_notes": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
        w.reports[rid] = row
        w.last_report = row
        return True

    def _list_by_status(status=None, *, limit=100):
        rows = list(w.reports.values())
        if status:
            rows = [r for r in rows if r["status"] == status]
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def _get(report_id):
        return w.reports.get(report_id)

    def _resolve(*, report_id, reviewer_id, action, notes):
        if action not in abuse_module.ACTION_TO_STATUS:
            return False
        new_status = abuse_module.ACTION_TO_STATUS[action]
        if new_status in ("actioned", "escalated") and not (notes or "").strip():
            return False
        r = w.reports.get(report_id)
        if not r:
            return False
        r["status"] = new_status
        r["reviewed_by"] = reviewer_id
        r["reviewed_at"] = "2026-05-07T00:00:01Z"
        r["action_notes"] = (notes or "").strip() or None
        return True

    def _count_pending():
        return sum(1 for r in w.reports.values() if r["status"] == "pending")

    monkeypatch.setattr(abuse_module, "create_report", _create)
    monkeypatch.setattr(abuse_module, "list_by_status", _list_by_status)
    monkeypatch.setattr(abuse_module, "get", _get)
    monkeypatch.setattr(abuse_module, "resolve", _resolve)
    monkeypatch.setattr(abuse_module, "count_pending", _count_pending)

    # ----- brands / profiles for the operator detail context preview -----
    monkeypatch.setattr(
        brands_module, "get_by_user_id", lambda uid: w.brands.get(uid)
    )
    monkeypatch.setattr(
        brands_module, "list_pending", lambda: []
    )
    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )

    # ----- dms preview helper used on detail page -----
    monkeypatch.setattr(
        dms_module, "list_messages",
        lambda thread_id, *, participant_id=None, limit=200: (
            w.thread_messages.get(thread_id, [])[:limit]
        ),
    )

    # ----- notifications -----
    def _notif_create(*, user_id, kind, title, body=None, link_path=None):
        if kind not in notifications_module.KINDS:
            return False
        w.notifications_sent.append({
            "user_id": user_id, "kind": kind, "title": title,
            "body": body, "link_path": link_path,
        })
        return True

    monkeypatch.setattr(notifications_module, "create", _notif_create)

    # ----- intel / operator console quiet -----
    from app.services import intel as intel_module
    monkeypatch.setattr(intel_module, "list_for_operator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "status_counts", lambda: {"draft": 0, "active": 0, "archived": 0, "scheduled": 0, "expired": 0})

    # ----- audit (resolve now writes to it) -----
    from app.services import audit as audit_module
    monkeypatch.setattr(audit_module, "record", lambda **kw: True)
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
# /report submission
# -----------------------------------------------------------------------------


def test_creator_can_report_dm_thread(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/report",
        data={
            "target_type": "dm_thread",
            "target_id": "t-1",
            "reason": "Persistent harassment after I declined the brief.",
            "return_to": "/creator/dm/b-1",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/dm/b-1?reported=1"
    assert world.last_report is not None
    assert world.last_report["reporter_id"] == "c-1"
    assert world.last_report["target_type"] == "dm_thread"
    assert world.last_report["target_id"] == "t-1"


def test_brand_can_report_profile(client, world):
    _signed_in(client, role="brand", user_id="b-1")
    r = client.post(
        "/report",
        data={
            "target_type": "profile",
            "target_id": "c-1",
            "reason": "Profile claims sponsorships from us that never existed.",
            "return_to": "/brand/creators/c-1",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/brand/creators/c-1?reported=1"
    assert world.last_report["reporter_id"] == "b-1"


def test_report_rejects_unknown_target_type(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/report",
        data={
            "target_type": "garbage",
            "target_id": "t-1",
            "reason": "Long enough reason here.",
            "return_to": "/creator/dm/x",
        },
    )
    assert r.status_code == 400
    assert world.last_report is None


def test_report_short_reason_is_recorded_as_failure(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/report",
        data={
            "target_type": "dm_thread",
            "target_id": "t-1",
            "reason": "no",                                   # too short
            "return_to": "/creator/dm/b-1",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/dm/b-1?reported=fail"
    assert world.last_report is None


def test_report_strips_offsite_redirects(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/report",
        data={
            "target_type": "dm_thread",
            "target_id": "t-1",
            "reason": "Long enough reason here.",
            "return_to": "https://evil.example/phish",        # off-site
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/?reported=1"


def test_report_drops_existing_reported_query(client, world):
    """Posting a second report shouldn't stack ?reported= flags."""
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/report",
        data={
            "target_type": "dm_thread",
            "target_id": "t-1",
            "reason": "Long enough reason here.",
            "return_to": "/creator/dm/b-1?reported=fail&keep=this",
        },
    )
    assert r.status_code == 303
    # Existing ?reported=fail dropped; ?keep=this retained; new flag appended.
    loc = r.headers["location"]
    assert "reported=1" in loc
    assert "reported=fail" not in loc
    assert "keep=this" in loc


def test_operators_cannot_report(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.post(
        "/report",
        data={
            "target_type": "profile", "target_id": "x",
            "reason": "Long enough reason here.", "return_to": "/operator",
        },
    )
    assert r.status_code == 403


def test_report_requires_auth(client, world):
    r = client.post(
        "/report",
        data={
            "target_type": "profile", "target_id": "x",
            "reason": "Long enough reason here.", "return_to": "/",
        },
    )
    assert r.status_code == 401


# -----------------------------------------------------------------------------
# Operator queue + detail
# -----------------------------------------------------------------------------


def test_operator_queue_lists_pending(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    # Seed reports
    for _ in range(2):
        rid = str(uuid4())
        world.reports[rid] = {
            "id": rid, "reporter_id": "c-1",
            "target_type": "dm_thread", "target_id": "t-1",
            "reason": "Long enough reason here for visibility.",
            "status": "pending", "reviewed_by": None, "reviewed_at": None,
            "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
        }
    rid_act = str(uuid4())
    world.reports[rid_act] = {
        "id": rid_act, "reporter_id": "c-2",
        "target_type": "profile", "target_id": "b-1",
        "reason": "Already-handled report.",
        "status": "actioned", "reviewed_by": "op-1",
        "reviewed_at": "2026-05-06T00:00:00Z",
        "action_notes": "Spoke to the brand.",
        "created_at": "2026-05-06T00:00:00Z",
    }

    r = client.get("/operator/abuse")
    assert r.status_code == 200
    assert r.text.lower().count("dm_thread".replace("_", " ")) >= 2

    r2 = client.get("/operator/abuse?tab=actioned")
    assert "Already-handled" in r2.text
    assert "Persistent" not in r2.text


def test_operator_detail_renders_thread_preview(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    rid = str(uuid4())
    tid = "t-1"
    world.reports[rid] = {
        "id": rid, "reporter_id": "c-1",
        "target_type": "dm_thread", "target_id": tid,
        "reason": "Pushy after I said no.",
        "status": "pending", "reviewed_by": None, "reviewed_at": None,
        "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    world.thread_messages[tid] = [
        {
            "id": "m1", "thread_id": tid, "sender_id": "b-1",
            "body": "Are you sure? we can pay 10x more.",
            "read_at": None, "created_at": "2026-05-07T00:00:00Z",
        },
    ]
    r = client.get(f"/operator/abuse/{rid}")
    assert r.status_code == 200
    assert "Pushy after I said no" in r.text
    assert "Are you sure?" in r.text


def test_operator_dismiss_no_notes(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    rid = str(uuid4())
    world.reports[rid] = {
        "id": rid, "reporter_id": "c-1",
        "target_type": "dm_thread", "target_id": "t-1",
        "reason": "Long enough reason here.",
        "status": "pending", "reviewed_by": None, "reviewed_at": None,
        "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(f"/operator/abuse/{rid}/dismiss", data={"notes": ""})
    assert r.status_code == 303
    assert r.headers["location"] == "/operator/abuse"
    assert world.reports[rid]["status"] == "dismissed"
    # Notification went to the reporter
    assert any(
        n["user_id"] == "c-1" and n["kind"] == "flag_update"
        for n in world.notifications_sent
    )


def test_operator_action_requires_notes(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    rid = str(uuid4())
    world.reports[rid] = {
        "id": rid, "reporter_id": "c-1",
        "target_type": "dm_thread", "target_id": "t-1",
        "reason": "Long enough reason.",
        "status": "pending", "reviewed_by": None, "reviewed_at": None,
        "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(f"/operator/abuse/{rid}/action", data={"notes": ""})
    assert r.status_code == 400
    assert "notes are required" in r.text.lower()
    assert world.reports[rid]["status"] == "pending"


def test_operator_action_with_notes_succeeds(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    rid = str(uuid4())
    world.reports[rid] = {
        "id": rid, "reporter_id": "c-1",
        "target_type": "dm_thread", "target_id": "t-1",
        "reason": "Long enough reason.",
        "status": "pending", "reviewed_by": None, "reviewed_at": None,
        "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(
        f"/operator/abuse/{rid}/action",
        data={"notes": "Brand warned; thread quarantined."},
    )
    assert r.status_code == 303
    assert world.reports[rid]["status"] == "actioned"
    assert world.reports[rid]["action_notes"].startswith("Brand warned")
    # Reporter notified
    n = world.notifications_sent[-1]
    assert n["user_id"] == "c-1"
    assert n["kind"] == "flag_update"
    assert "Brand warned" in (n["body"] or "")


def test_operator_escalate_requires_notes(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    rid = str(uuid4())
    world.reports[rid] = {
        "id": rid, "reporter_id": "c-1",
        "target_type": "dm_thread", "target_id": "t-1",
        "reason": "Long enough reason.",
        "status": "pending", "reviewed_by": None, "reviewed_at": None,
        "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(f"/operator/abuse/{rid}/escalate", data={"notes": ""})
    assert r.status_code == 400


def test_operator_unknown_action_400(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    rid = str(uuid4())
    world.reports[rid] = {
        "id": rid, "reporter_id": "c-1",
        "target_type": "dm_thread", "target_id": "t-1",
        "reason": "Long enough reason.",
        "status": "pending", "reviewed_by": None, "reviewed_at": None,
        "action_notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(f"/operator/abuse/{rid}/banhammer", data={"notes": "x"})
    assert r.status_code == 400


def test_operator_detail_404_for_unknown_report(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get(f"/operator/abuse/{uuid4()}")
    assert r.status_code == 404


def test_operator_abuse_requires_operator_role(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/operator/abuse")
    assert r.status_code == 403
