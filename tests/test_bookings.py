"""Booking CRUD tests — calendar list, create, edit, cancel."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import abuse as abuse_module
from app.services import bookings as bookings_module
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import notifications as notifications_module


class FakeWorld:
    def __init__(self):
        self.bookings: dict[str, dict[str, Any]] = {}


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    def _list_for_user(uid, *, horizon="all", limit=200):
        rows = [b for b in w.bookings.values() if b["user_id"] == uid and b["status"] != "cancelled"]
        rows.sort(key=lambda b: b["starts_at"])
        return rows

    def _get(bid):
        return w.bookings.get(bid)

    def _create(*, user_id, payload):
        bid = str(uuid4())
        w.bookings[bid] = {
            **payload, "id": bid, "user_id": user_id,
            "created_at": "2026-05-07T00:00:00Z",
        }
        return bid

    def _update(bid, *, user_id, payload):
        b = w.bookings.get(bid)
        if not b or b["user_id"] != user_id:
            return False
        b.update(payload)
        return True

    def _cancel(bid, *, user_id):
        return _update(bid, user_id=user_id, payload={"status": "cancelled"})

    monkeypatch.setattr(bookings_module, "list_for_user", _list_for_user)
    monkeypatch.setattr(bookings_module, "get", _get)
    monkeypatch.setattr(bookings_module, "create", _create)
    monkeypatch.setattr(bookings_module, "update", _update)
    monkeypatch.setattr(bookings_module, "cancel", _cancel)

    # Quiet everything else
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


def test_calendar_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-1", "title": "Dinner at Boia",
        "type": "restaurant", "starts_at": "2099-05-07T19:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": "Boia De",
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/creator/calendar")
    assert r.status_code == 200
    assert "Dinner at Boia" in r.text


def test_calendar_create(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar",
        data={
            "title": "Brand call",
            "type": "brand",
            "starts_at": "2099-05-08T10:00",
            "ends_at": "",
            "notes": "Pitch the SS26 capsule.",
            "venue_name": "Zoom",
            "status": "confirmed",
        },
    )
    assert r.status_code == 303
    assert len(world.bookings) == 1
    b = next(iter(world.bookings.values()))
    assert b["title"] == "Brand call"
    assert b["type"] == "brand"


def test_calendar_create_rejects_missing_title(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar",
        data={"type": "event", "starts_at": "2099-05-08T10:00"},
    )
    assert r.status_code == 400


def test_calendar_create_rejects_unknown_type(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/calendar",
        data={"title": "x", "type": "garbage", "starts_at": "2099-05-08T10:00"},
    )
    assert r.status_code == 400


def test_calendar_detail_only_owner(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-other", "title": "Theirs",
        "type": "event", "starts_at": "2099-05-08T10:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": None,
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.get(f"/creator/calendar/{bid}")
    assert r.status_code == 404


def test_calendar_cancel(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    bid = str(uuid4())
    world.bookings[bid] = {
        "id": bid, "user_id": "c-1", "title": "Skip this",
        "type": "event", "starts_at": "2099-05-08T10:00:00Z",
        "ends_at": None, "status": "confirmed", "venue_name": None,
        "notes": None, "created_at": "2026-05-07T00:00:00Z",
    }
    r = client.post(f"/creator/calendar/{bid}/cancel")
    assert r.status_code == 303
    assert world.bookings[bid]["status"] == "cancelled"


def test_calendar_requires_creator(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/calendar")
    assert r.status_code == 403
