"""Operator member roster + per-user notes + audit log tests, plus the
404 / 500 HTML error handlers from the polish pass."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import abuse as abuse_module
from app.services import audit as audit_module
from app.services import intel as intel_module
from app.services import members as members_module
from app.services import notifications as notifications_module
from app.services import operator_notes as operator_notes_module
from app.services import profiles as profiles_module


class FakeWorld:
    def __init__(self):
        self.users: dict[str, dict[str, Any]] = {}
        self.notes_by_user: dict[str, list[dict[str, Any]]] = {}
        self.audit_rows: list[dict[str, Any]] = []
        self.last_note: dict[str, Any] | None = None
        self.last_audit: dict[str, Any] | None = None


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    def _list_users(*, role=None, page=1, page_size=100):
        rows = list(w.users.values())
        if role:
            rows = [r for r in rows if r["role"] == role]
        return rows, len(rows)

    def _get_user(uid):
        return w.users.get(uid)

    def _annotate(user):
        user["profile"] = None
        return user

    monkeypatch.setattr(members_module, "list_users", _list_users)
    monkeypatch.setattr(members_module, "get_user", _get_user)
    monkeypatch.setattr(members_module, "annotate_with_profile", _annotate)

    def _notes_list(uid, *, limit=50):
        return w.notes_by_user.get(uid, [])

    def _notes_create(*, target_user_id, author_user_id, body):
        if not body:
            return False
        n = {
            "id": str(uuid4()),
            "target_user_id": target_user_id,
            "author_user_id": author_user_id,
            "body": body,
            "created_at": "2026-05-07T00:00:00Z",
        }
        w.notes_by_user.setdefault(target_user_id, []).append(n)
        w.last_note = n
        return True

    monkeypatch.setattr(operator_notes_module, "list_for_user", _notes_list)
    monkeypatch.setattr(operator_notes_module, "create", _notes_create)

    def _audit_record(*, actor_user_id, action, target_type=None, target_id=None, notes=None):
        w.last_audit = {
            "actor_user_id": actor_user_id, "action": action,
            "target_type": target_type, "target_id": target_id, "notes": notes,
        }
        return True

    monkeypatch.setattr(audit_module, "record", _audit_record)
    monkeypatch.setattr(audit_module, "list_recent", lambda *, limit=200: w.audit_rows)

    # Quiet everything else used by the operator console
    monkeypatch.setattr(notifications_module, "create", lambda **kw: True)
    monkeypatch.setattr(intel_module, "list_for_operator", lambda **kw: [])
    monkeypatch.setattr(profiles_module, "get_creator_profile", lambda uid: None)
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


# Member roster

def test_members_list_renders(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.users["c-1"] = {
        "id": "c-1", "email": "anna@example.com", "role": "creator",
        "is_active": True, "last_seen_at": None,
        "created_at": "2026-05-01T00:00:00Z",
    }
    world.users["op-2"] = {
        "id": "op-2", "email": "ops@example.com", "role": "operator",
        "is_active": True, "last_seen_at": None,
        "created_at": "2026-05-02T00:00:00Z",
    }
    r = client.get("/operator/members")
    assert r.status_code == 200
    assert "anna@example.com" in r.text
    assert "ops@example.com" in r.text


def test_members_list_role_filter(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.users["c-1"] = {
        "id": "c-1", "email": "anna@example.com", "role": "creator",
        "is_active": True, "last_seen_at": None, "created_at": "2026-05-01T00:00:00Z",
    }
    world.users["op-2"] = {
        "id": "op-2", "email": "ops@example.com", "role": "operator",
        "is_active": True, "last_seen_at": None, "created_at": "2026-05-02T00:00:00Z",
    }
    r = client.get("/operator/members?role=creator")
    assert "anna@example.com" in r.text
    assert "ops@example.com" not in r.text


def test_member_detail_renders_with_notes(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.users["c-1"] = {
        "id": "c-1", "email": "anna@example.com", "role": "creator",
        "is_active": True, "last_seen_at": None, "created_at": "2026-05-01T00:00:00Z",
    }
    world.notes_by_user["c-1"] = [{
        "id": "n-1", "target_user_id": "c-1", "author_user_id": "op-1",
        "body": "Watch list — recently flagged for spam.",
        "created_at": "2026-05-06T00:00:00Z",
    }]
    r = client.get("/operator/members/c-1")
    assert r.status_code == 200
    assert "anna@example.com" in r.text
    assert "Watch list" in r.text


def test_member_detail_404(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/operator/members/missing")
    assert r.status_code == 404


def test_member_note_create(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.users["c-1"] = {
        "id": "c-1", "email": "anna@example.com", "role": "creator",
        "is_active": True, "last_seen_at": None, "created_at": "2026-05-01T00:00:00Z",
    }
    r = client.post(
        "/operator/members/c-1/note",
        data={"body": "Onboarded smoothly; relaxed about limits."},
    )
    assert r.status_code == 303
    assert world.last_note is not None
    assert world.last_note["target_user_id"] == "c-1"
    assert world.last_audit is not None
    assert world.last_audit["action"] == "operator_note.create"


def test_member_note_rejects_empty(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.users["c-1"] = {
        "id": "c-1", "email": "x@example.com", "role": "creator",
        "is_active": True, "last_seen_at": None, "created_at": "2026-05-01T00:00:00Z",
    }
    r = client.post("/operator/members/c-1/note", data={"body": "  "})
    assert r.status_code == 400


def test_members_requires_operator(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/operator/members")
    assert r.status_code == 403


# Audit

def test_audit_list_renders(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.audit_rows = [
        {
            "id": "a-1", "actor_user_id": "op-1", "action": "intel.publish",
            "target_type": "intel", "target_id": "i-1", "notes": None,
            "created_at": "2026-05-07T10:00:00Z",
        },
    ]
    r = client.get("/operator/audit")
    assert r.status_code == 200
    assert "intel.publish" in r.text


def test_audit_requires_operator(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/operator/audit")
    assert r.status_code == 403


# 404 / error handler

def test_unknown_route_renders_html_404(client, world):
    r = client.get("/this-route-does-not-exist")
    assert r.status_code == 404
    # Should be the HTML page, not the FastAPI default JSON
    assert "Page not found" in r.text
    assert r.headers["content-type"].startswith("text/html")
