"""Posting tests — creator CRUD + operator takedown.

v1 is creator-only. Brand-side jobs browse (signed in as a verified
brand, hitting /brand/jobs) shipped on the brand-side-v1.5 branch.

Stubs the jobs service; the routes integrate cleanly because every layer
is gated by require_role and the service contract is small.
"""

from __future__ import annotations

from datetime import datetime, timedelta
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
from app.services import jobs as jobs_module
from app.services import network as network_module
from app.services import notifications as notifications_module
from app.services import profiles as profiles_module
from app.services import views as views_module


class FakeWorld:
    def __init__(self):
        self.creators: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, dict[str, Any]] = {}
        self.connections: dict[tuple[str, str], dict[str, Any]] = {}
        self.notifs: list[dict[str, Any]] = []
        # listing id -> has_dependents() answer (True / None); default False.
        self.dependents: dict[str, bool | None] = {}
        self.delete_calls: list[str] = []

    def add_creator(self, *, user_id, **kw):
        self.creators[user_id] = {
            "user_id": user_id,
            "full_name": kw.get("full_name", f"Creator {user_id}"),
            "instagram_handle": kw.get("instagram_handle", user_id),
            "niches": kw.get("niches", ["food"]),
            "tier": kw.get("tier", "basic"),
            "follower_range": kw.get("follower_range", "10-50k"),
            "onboarding_completed_at": "2026-05-07T00:00:00Z",
            "engagement_range": None, "creator_tenure": None,
            "location_city": None, "location_region": None,
            "primary_platform": "Instagram",
            "content_formats": ["reels"], "hard_limits": [], "bio": None,
        }
        return self.creators[user_id]

    def add_listing(self, *, poster, **kw):
        lid = str(uuid4())
        self.listings[lid] = {
            "id": lid,
            "poster_user_id": poster,
            "title": kw.get("title", "Untitled"),
            "description": kw.get("description", "Body."),
            "listing_type": kw.get("listing_type", "collab"),
            "compensation_text": kw.get("compensation_text"),
            "target_niches": kw.get("target_niches", []),
            "deadline": kw.get("deadline"),
            "is_active": kw.get("is_active", True),
            "is_taken_down": kw.get("is_taken_down", False),
            "taken_down_reason": None,
            "taken_down_by": None, "taken_down_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
        return self.listings[lid]


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    monkeypatch.setattr(
        profiles_module, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creators_by_ids",
        lambda ids: {uid: w.creators[uid] for uid in ids if uid in w.creators},
    )

    # ----- jobs service -----
    def _list_active(*, niche=None, limit=100):
        rows = [
            lst for lst in w.listings.values()
            if lst["is_active"] and not lst["is_taken_down"]
        ]
        if niche:
            rows = [lst for lst in rows if niche in (lst.get("target_niches") or [])]
        rows.sort(key=lambda lst: lst["created_at"], reverse=True)
        return rows[:limit]

    def _list_by_poster(uid):
        return [lst for lst in w.listings.values() if lst["poster_user_id"] == uid]

    def _list_for_operator(*, taken_down=None):
        rows = list(w.listings.values())
        if taken_down is True:
            rows = [lst for lst in rows if lst["is_taken_down"]]
        elif taken_down is False:
            rows = [lst for lst in rows if not lst["is_taken_down"]]
        return rows

    def _get(lid):
        return w.listings.get(lid)

    def _create(*, poster_id, poster_role, payload):
        lid = str(uuid4())
        w.listings[lid] = {
            **payload, "id": lid, "poster_user_id": poster_id,
            "poster_role": poster_role,
            "is_taken_down": False, "taken_down_reason": None,
            "taken_down_by": None, "taken_down_at": None,
            "created_at": "2026-05-07T00:00:00Z",
        }
        return lid

    def _update(lid, payload, *, poster_id):
        listing = w.listings.get(lid)
        if listing is None or listing.get("poster_user_id") != poster_id:
            return False
        listing.update(payload)
        return True

    def _deactivate(lid, *, poster_id):
        listing = w.listings.get(lid)
        if not listing or listing["poster_user_id"] != poster_id:
            return False
        listing["is_active"] = False
        return True

    def _delete(lid, *, poster_id):
        w.delete_calls.append(lid)
        listing = w.listings.get(lid)
        if not listing or listing["poster_user_id"] != poster_id:
            return False
        del w.listings[lid]
        return True

    def _take_down(*, listing_id, operator_id, reason):
        if not reason:
            return False
        listing = w.listings.get(listing_id)
        if not listing:
            return False
        listing.update({
            "is_taken_down": True, "taken_down_reason": reason,
            "taken_down_by": operator_id, "taken_down_at": "2026-05-07T00:00:01Z",
            "is_active": False,
        })
        return True

    monkeypatch.setattr(jobs_module, "list_active", _list_active)
    monkeypatch.setattr(jobs_module, "list_by_poster", _list_by_poster)
    monkeypatch.setattr(jobs_module, "list_for_operator", _list_for_operator)
    monkeypatch.setattr(jobs_module, "get", _get)
    monkeypatch.setattr(jobs_module, "create", _create)
    monkeypatch.setattr(jobs_module, "update", _update)
    monkeypatch.setattr(jobs_module, "deactivate", _deactivate)
    monkeypatch.setattr(jobs_module, "delete", _delete)
    monkeypatch.setattr(jobs_module, "has_dependents", lambda lid: w.dependents.get(lid, False))
    monkeypatch.setattr(jobs_module, "take_down", _take_down)

    # ----- network: connection lookup for "can DM" gate -----
    def _get_connection_between(a, b):
        return w.connections.get((min(a, b), max(a, b)))

    monkeypatch.setattr(network_module, "get_connection_between", _get_connection_between)

    # ----- notifications + dms + others quiet -----
    def _create_notif(*, user_id, kind, title, body=None, link_path=None):
        if kind not in notifications_module.KINDS:
            return False
        w.notifs.append({
            "user_id": user_id, "kind": kind, "title": title,
            "body": body, "link_path": link_path,
        })
        return True

    monkeypatch.setattr(notifications_module, "create", _create_notif)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)
    monkeypatch.setattr(views_module, "record_view", lambda *, viewer_id, viewed_id: True)

    from app.services import audit as audit_module
    monkeypatch.setattr(audit_module, "record", lambda **kw: True)

    return w


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def _signed_in(client, *, role, user_id):
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _deadline(days: int = 7) -> str:
    return (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M")


# -----------------------------------------------------------------------------
# Creator-side job CRUD
# -----------------------------------------------------------------------------


def test_creator_jobs_board_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    world.add_listing(poster="c-2", title="Need a UGC partner")
    r = client.get("/creator/jobs")
    assert r.status_code == 200
    assert "Need a UGC partner" in r.text
    assert "postings" in r.text
    assert "creator postings" not in r.text


def test_creator_jobs_create(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "title": "Looking for a videographer",
            "description": "Half-day shoot next week.",
            "listing_type": "hiring",
            "compensation_text": "$300",
            "deadline": _deadline(),
            "target_niches": ["fashion", "food"],
        },
    )
    assert r.status_code == 303
    assert r.headers["location"].startswith("/creator/jobs/")
    # One posting in the world
    assert len(world.listings) == 1
    listing = next(iter(world.listings.values()))
    assert listing["is_active"] is True
    assert listing["poster_user_id"] == "c-1"
    assert listing["poster_role"] == "creator"


def test_creator_jobs_create_rejects_non_money_compensation(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "title": "Looking for a videographer",
            "description": "Half-day shoot next week.",
            "listing_type": "hiring",
            "compensation_text": "TFP / negotiable",
            "deadline": _deadline(),
        },
    )
    assert r.status_code == 400
    assert "Posting compensation must be a dollar amount" in r.text
    assert world.listings == {}


def test_creator_jobs_create_rejects_deadline_after_14_days(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "title": "Looking for a videographer",
            "description": "Half-day shoot next week.",
            "listing_type": "hiring",
            "compensation_text": "$300",
            "deadline": _deadline(15),
        },
    )
    assert r.status_code == 400
    assert "Posting deadline must be within 14 days" in r.text
    assert world.listings == {}


def test_creator_jobs_create_rejects_missing_deadline(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "title": "Looking for a videographer",
            "description": "Half-day shoot next week.",
            "listing_type": "hiring",
            "compensation_text": "$300",
        },
    )
    assert r.status_code == 400
    assert "Posting deadline is required" in r.text
    assert world.listings == {}


def test_creator_jobs_create_rejects_ugc_gig_type(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "title": "Looking for a videographer",
            "description": "Half-day shoot next week.",
            "listing_type": "ugc_gig",
            "compensation_text": "$300",
            "deadline": _deadline(),
        },
    )
    assert r.status_code == 400
    assert "Pick a posting type" in r.text
    assert world.listings == {}


def test_creator_jobs_create_rejects_missing_title(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.post(
        "/creator/jobs",
        data={
            "description": "x",
            "listing_type": "collab",
            "compensation_text": "$250",
            "deadline": _deadline(),
        },
    )
    assert r.status_code == 400
    assert world.listings == {}


def test_creator_jobs_edit_only_owner(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-2", title="Theirs")
    r = client.get(f"/creator/jobs/{listing['id']}/edit")
    assert r.status_code == 403


def test_creator_jobs_form_hides_ugc_gig_and_back_link(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.get("/creator/jobs/new")
    assert r.status_code == 200
    assert "ugc_gig" not in r.text
    assert "my postings" not in r.text
    assert "brand deal" in r.text
    assert "brand_deal" in r.text
    assert ">post<" in r.text
    assert "post posting" not in r.text
    assert "required. must be within 14 days." in r.text


def test_creator_jobs_old_ugc_gig_record_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Legacy", listing_type="ugc_gig")
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "Legacy" in r.text


def test_opportunity_detail_renders_stored_fields_without_application(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Maya Creator")
    listing = world.add_listing(
        poster="c-2", title="Real UGC brief", description="Two videos\nDue Friday",
        listing_type="ugc_gig", compensation_text="gifted / product",
        target_niches=["food", "wellness"], deadline="2026-10-15T23:59:59+00:00",
    )
    listing.update(poster_role="creator", location_city="Miami")
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    for text in ("Real UGC brief", "Maya Creator", "creator", "gifted / product",
                 "Two videos", "Due Friday", "Miami", "food", "wellness"):
        assert text in r.text
    # Step 5B landed the real Apply flow — the old disabled-button
    # affordance was replaced with a functional link to the dedicated
    # application form. Guard against a regression that reintroduces
    # the disabled button, and lock in that the new link renders.
    assert '<button type="button" class="btn btn-lime" disabled>Apply</button>' not in r.text
    assert f'href="/creator/jobs/{listing["id"]}/apply"' in r.text
    # Nothing on the detail page itself posts; the apply form lives
    # on a dedicated route.
    assert 'action="/creator/jobs/' not in r.text
    assert "deliverables</h2>" not in r.text


def test_opportunity_detail_brand_identity_and_budget(client, world, monkeypatch):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    monkeypatch.setattr(profiles_module, "get_brand_profile", lambda uid: {
        "company_name": "Actual Brand", "logo_url": "https://example.com/logo.png",
    } if uid == "b-1" else None)
    listing = world.add_listing(poster="b-1", title="Campaign", compensation_text=None)
    listing.update(poster_role="brand", budget_min=1000, budget_max=2000)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "Actual Brand" in r.text
    assert "$1000" in r.text and "$2000" in r.text
    assert "/creator/network/b-1" not in r.text
    assert "location</dt>" not in r.text
    assert "closes</dt>" not in r.text


def test_opportunity_detail_missing_listing_returns_404(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    r = client.get(f"/creator/jobs/{uuid4()}")
    assert r.status_code == 404


@pytest.mark.parametrize("hidden_fields", [
    {"discovery_eligible": False},
    {"expires_at": "2020-01-01T00:00:00+00:00"},
    {"is_active": False},
    {"is_taken_down": True},
])
def test_opportunity_detail_hides_ineligible_nonowner(client, world, hidden_fields):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-2")
    listing.update(hidden_fields)
    assert client.get(f"/creator/jobs/{listing['id']}").status_code == 404


def test_creator_jobs_detail_dm_gate_when_unconnected(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2", full_name="Other")
    listing = world.add_listing(poster="c-2", title="Open")
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "Connect first" in r.text


def test_creator_jobs_detail_dm_unlocks_when_connected(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-2", title="Open")
    world.connections[("c-1", "c-2")] = {"status": "accepted"}
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "Open DM" in r.text


def test_creator_jobs_close(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Mine")
    r = client.post(f"/creator/jobs/{listing['id']}/close")
    assert r.status_code == 303
    assert world.listings[listing["id"]]["is_active"] is False


def test_creator_jobs_delete_owner_only(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Mine")
    r = client.post(f"/creator/jobs/{listing['id']}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == "/creator/jobs"
    assert listing["id"] not in world.listings


def test_creator_jobs_delete_rejects_non_owner(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-2", title="Theirs")
    r = client.post(f"/creator/jobs/{listing['id']}/delete")
    assert r.status_code == 403
    assert listing["id"] in world.listings
    assert world.delete_calls == []


def test_creator_jobs_delete_refused_when_people_applied(client, world):
    """Applications, offers and deals cascade from the listing, so a
    posting anyone applied to is never deleted; the edit page explains."""
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Mine")
    world.dependents[listing["id"]] = True
    r = client.post(f"/creator/jobs/{listing['id']}/delete")
    assert r.status_code == 303
    assert r.headers["location"] == (
        f"/creator/jobs/{listing['id']}/edit?delete=blocked#delete-posting"
    )
    assert listing["id"] in world.listings
    assert world.delete_calls == []

    page = client.get(r.headers["location"])
    assert page.status_code == 200
    assert "can&#39;t be deleted" in page.text or "can't be deleted" in page.text
    assert 'href="/creator/jobs/mine"' in page.text


def test_creator_jobs_delete_refused_when_dependents_unknown(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Mine")
    world.dependents[listing["id"]] = None
    r = client.post(f"/creator/jobs/{listing['id']}/delete")
    assert r.status_code == 303
    assert r.headers["location"].endswith("?delete=unavailable#delete-posting")
    assert listing["id"] in world.listings
    assert world.delete_calls == []
    page = client.get(r.headers["location"])
    assert "nothing was deleted" in page.text


def test_creator_jobs_delete_unknown_listing_404(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    for bad in (str(uuid4()), "not-a-uuid"):
        r = client.post(f"/creator/jobs/{bad}/delete")
        assert r.status_code == 404
    assert world.delete_calls == []


def test_creator_jobs_edit_delete_button_confirms_without_inline_js(client, world):
    """Inline onclick handlers are blocked by the CSP (script-src 'self'),
    which used to let delete fire with no confirmation. The button asks via
    data-confirm, handled by an external same-origin script."""
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", title="Mine")
    page = client.get(f"/creator/jobs/{listing['id']}/edit")
    assert page.status_code == 200
    form = page.text.split('id="delete-posting"', 1)[1].split("</form>", 1)[0]
    assert "data-confirm=" in form
    assert "onclick" not in form
    assert "/static/js/confirm_submit.js?v=" in page.text
    assert "script-src 'self'" in page.headers["content-security-policy"]


def test_no_template_uses_inline_event_handlers():
    """The CSP blocks inline handlers, so any on*="..." attribute silently
    does nothing in the browser."""
    import re
    from pathlib import Path

    root = Path(__file__).parents[1] / "app" / "templates"
    offenders = [
        f"{path.relative_to(root)}: {match.group(0)}"
        for path in root.rglob("*.html")
        for match in re.finditer(r"\son[a-z]+\s*=\s*[\"']", path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


def test_confirm_script_cancels_and_never_double_asks():
    from pathlib import Path

    js = (Path(__file__).parents[1] / "app/static/js/confirm_submit.js").read_text(encoding="utf-8")
    assert 'document.addEventListener("submit"' in js
    assert "window.confirm(" in js
    assert "event.preventDefault()" in js
    assert 'hasAttribute("data-submitting")' in js


class _ChildTable:
    def __init__(self, rows, calls, table, error=None):
        self._rows, self._calls, self._table, self._error = rows, calls, table, error

    def select(self, cols):
        self._calls.append((self._table, "select", cols))
        return self

    def eq(self, col, val):
        self._calls.append((self._table, "eq", col, val))
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def limit(self, n):
        self._calls.append((self._table, "limit", n))
        return self

    def execute(self):
        if self._error is not None:
            raise self._error
        return type("R", (), {"data": self._rows[:1]})()


class _ChildClient:
    def __init__(self, tables, error_on=None):
        self.tables, self.calls, self.error_on = tables, [], error_on

    def table(self, name):
        from postgrest.exceptions import APIError

        err = APIError({"message": "boom"}) if name == self.error_on else None
        return _ChildTable(list(self.tables.get(name, [])), self.calls, name, err)


@pytest.mark.parametrize("child", [
    "creator_job_applications", "creator_job_offers", "creator_job_deals",
])
def test_has_dependents_finds_any_cascading_child(monkeypatch, child):
    from app.core import supabase_client

    client = _ChildClient({child: [{"id": "x", "listing_id": "L1"}]})
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: client)
    assert jobs_module.has_dependents("L1") is True
    assert jobs_module.has_dependents("L2") is False


def test_has_dependents_reads_only_ids_by_listing(monkeypatch):
    from app.core import supabase_client

    client = _ChildClient({})
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: client)
    assert jobs_module.has_dependents("L1") is False
    tables = [c[0] for c in client.calls if c[1] == "select"]
    assert tables == ["creator_job_applications", "creator_job_offers", "creator_job_deals"]
    assert all(c[2] == "id" for c in client.calls if c[1] == "select")
    assert all(c[2:] == ("listing_id", "L1") for c in client.calls if c[1] == "eq")


def test_has_dependents_unknown_on_read_failure(monkeypatch):
    from app.core import supabase_client

    client = _ChildClient({}, error_on="creator_job_offers")
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: client)
    assert jobs_module.has_dependents("L1") is None


def test_creator_jobs_404_taken_down(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", is_taken_down=True)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 404


def test_creator_jobs_closed_listing_404_for_non_owner(client, world):
    """Other creators shouldn't be able to read soft-closed postings
    by guessing the UUID (AUDIT.md M4)."""
    _signed_in(client, role="creator", user_id="c-2")
    world.add_creator(user_id="c-1")
    world.add_creator(user_id="c-2")
    listing = world.add_listing(poster="c-1", is_active=False)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 404


def test_creator_jobs_closed_listing_visible_to_owner(client, world):
    """Posters still see their own closed postings so they can re-open."""
    _signed_in(client, role="creator", user_id="c-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1", is_active=False)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200


# -----------------------------------------------------------------------------
# Operator: take down
# -----------------------------------------------------------------------------


def test_operator_jobs_list_renders(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_creator(user_id="c-1", full_name="Anna")
    world.add_listing(poster="c-1", title="Open one")
    r = client.get("/operator/jobs")
    assert r.status_code == 200
    assert "Open one" in r.text


def test_operator_takedown_requires_reason(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1")
    r = client.post(f"/operator/jobs/{listing['id']}/takedown", data={"reason": ""})
    assert r.status_code == 400
    assert world.listings[listing["id"]]["is_taken_down"] is False


def test_operator_takedown_succeeds_and_notifies(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    world.add_creator(user_id="c-1")
    listing = world.add_listing(poster="c-1")
    r = client.post(
        f"/operator/jobs/{listing['id']}/takedown",
        data={"reason": "Misleading comp claim."},
    )
    assert r.status_code == 303
    assert world.listings[listing["id"]]["is_taken_down"] is True
    assert any(
        n["user_id"] == "c-1" and n["kind"] == "flag_update"
        for n in world.notifs
    )


def test_operator_jobs_requires_operator(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.get("/operator/jobs")
    assert r.status_code == 403
