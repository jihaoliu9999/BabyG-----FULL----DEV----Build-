"""Cross-user authorization regression net.

For every owner-scoped detail / edit route, a different signed-in user
must get 404 (or 403). Phase 2 will add more such routes (agent
endpoints, integration callbacks); having this matrix in place catches
regressions where a developer forgets to add the "is this mine?" check.

Pattern: seed a resource owned by `other`, sign in as `me`, hit the
detail / edit / mutation URL. Expected: 4xx, NOT 200 with the row's
contents.

Already covered case-by-case elsewhere (kept here for completeness):
  * /creator/calendar/{id}     → test_bookings.test_calendar_detail_only_owner
  * /creator/jobs/{id}/edit    → test_jobs.test_creator_jobs_edit_only_owner
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import bookings as bookings_module
from app.services import jobs as jobs_module
from app.services import profiles as profiles_module


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str, user_id: str) -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


@pytest.fixture()
def world(monkeypatch):
    """Lightweight world: just enough stubs for ownership checks to fire."""
    state: dict = {
        "bookings": {},
        "listings": {},
        "creators": {},
    }
    monkeypatch.setattr(
        bookings_module, "get", lambda bid: state["bookings"].get(bid)
    )
    monkeypatch.setattr(
        jobs_module, "get", lambda lid: state["listings"].get(lid)
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creator_profile",
        lambda uid: state["creators"].get(uid),
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creators_by_ids",
        lambda ids: {u: state["creators"][u] for u in ids if u in state["creators"]},
    )
    return state


# ---------- bookings ----------


def test_other_creators_booking_detail_404(client, world):
    bid = str(uuid4())
    world["bookings"][bid] = {
        "id": bid, "user_id": "owner-id", "title": "Theirs",
        "type": "event", "starts_at": "2099-05-08T10:00:00Z",
        "ends_at": None, "status": "confirmed",
        "venue_name": None, "notes": None,
        "created_at": "2026-05-07T00:00:00Z",
    }
    _signed_in(client, role="creator", user_id="other-id")
    r = client.get(f"/creator/calendar/{bid}")
    assert r.status_code == 404


def test_other_creators_booking_edit_form_404(client, world):
    bid = str(uuid4())
    world["bookings"][bid] = {
        "id": bid, "user_id": "owner-id", "title": "Theirs",
        "type": "event", "starts_at": "2099-05-08T10:00:00Z",
        "ends_at": None, "status": "confirmed",
        "venue_name": None, "notes": None,
        "created_at": "2026-05-07T00:00:00Z",
    }
    _signed_in(client, role="creator", user_id="other-id")
    r = client.get(f"/creator/calendar/{bid}/edit")
    assert r.status_code == 404


# ---------- jobs ----------


def test_other_creators_job_edit_form_403(client, world):
    """Editing someone else's listing must 403 (route checks ownership
    on the edit-form GET)."""
    lid = str(uuid4())
    world["listings"][lid] = {
        "id": lid, "poster_user_id": "owner-id",
        "title": "x", "description": "x",
        "listing_type": "collab", "compensation_text": None,
        "target_niches": [], "deadline": None,
        "is_active": True, "is_taken_down": False,
        "created_at": "2026-05-07T00:00:00Z",
    }
    _signed_in(client, role="creator", user_id="other-id")
    r = client.get(f"/creator/jobs/{lid}/edit")
    assert r.status_code == 403


def test_other_creators_closed_job_detail_404(client, world):
    """Closed listings are invisible to non-owners (M4)."""
    lid = str(uuid4())
    world["listings"][lid] = {
        "id": lid, "poster_user_id": "owner-id",
        "title": "x", "description": "x",
        "listing_type": "collab", "compensation_text": None,
        "target_niches": [], "deadline": None,
        "is_active": False, "is_taken_down": False,
        "created_at": "2026-05-07T00:00:00Z",
    }
    _signed_in(client, role="creator", user_id="other-id")
    r = client.get(f"/creator/jobs/{lid}")
    assert r.status_code == 404


# ---------- /report (H3 standing) ----------


def test_other_users_dm_thread_report_rejected(client, world, monkeypatch):
    """Reporting a thread you're not in must NOT save a real report.
    Locks in AUDIT.md H3."""
    from app.services import abuse as abuse_module
    from app.services import dms as dms_module

    saved: list = []

    def _create(**kw):
        saved.append(kw)
        return True

    monkeypatch.setattr(abuse_module, "create_report", _create)
    # Pretend the reporter is NOT a participant.
    monkeypatch.setattr(dms_module, "_is_participant", lambda tid, uid: False)

    _signed_in(client, role="creator", user_id="other-id")
    r = client.post(
        "/report",
        data={
            "target_type": "dm_thread",
            "target_id": "11111111-1111-1111-1111-111111111111",
            "reason": "long enough reason to pass the validator",
            "return_to": "/creator",
        },
    )
    assert r.status_code == 303
    assert "reported=fail" in r.headers["location"]
    assert saved == []  # no row landed in the operator queue
