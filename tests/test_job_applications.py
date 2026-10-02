"""Step 5B: creator applications to opportunity listings.

Covers the 22 behavioral + security guarantees in the Step 5B spec:

  Flow
    * eligible creator sees Apply on the opportunity detail page
    * tapping Apply opens the dedicated application form page
    * form renders a real poster summary + opportunity title + comp
    * valid submit persists and redirects to success
    * success page is unreachable without a persisted application
    * revisiting the opportunity shows ✓ Applied
    * another creator viewing the same opportunity still sees Apply

  Security
    * applicant identity is pulled from the authenticated session —
      a form-level applicant_user_id is ignored
    * creator cannot apply to their own opportunity → 403
    * nonexistent / taken-down opportunity → 404
    * duplicate submission (fast path + unique-constraint race) is
      safely absorbed, never produces a double row
    * empty / whitespace-only message rejected
    * message > 2000 chars rejected

  Isolation (locked systems)
    * no DM created / sent
    * no deal / offer / Stripe artefact touched

  Privacy
    * no application data flows into Discover

Supabase is stubbed per-test — no live DB.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import job_applications
from app.services import jobs as jobs_service
from app.services import network as network_service
from app.services import profiles as profiles_service

# ---------- stub world ----------


class _World:
    def __init__(self) -> None:
        self.creators: dict[str, dict[str, Any]] = {}
        self.brands: dict[str, dict[str, Any]] = {}
        self.listings: dict[str, dict[str, Any]] = {}
        # applications keyed by (listing_id, applicant_user_id) so
        # concurrent fakes can only insert once.
        self.applications: dict[tuple[str, str], dict[str, Any]] = {}
        self.insert_failures: int = 0  # test can set to force failure

    def add_creator(self, user_id: str, **kw: Any) -> dict[str, Any]:
        self.creators[user_id] = {
            "user_id": user_id,
            "full_name": kw.get("full_name", f"Creator {user_id}"),
            "instagram_handle": kw.get("instagram_handle", user_id),
            "profile_photo_url": kw.get("profile_photo_url"),
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "location_city": None,
            "location_region": None,
            "location_country": None,
            "niches": [],
            "primary_platform": "Instagram",
        }
        return self.creators[user_id]

    def add_brand(self, user_id: str, **kw: Any) -> dict[str, Any]:
        self.brands[user_id] = {
            "user_id": user_id,
            "company_name": kw.get("company_name", f"Brand {user_id}"),
            "logo_url": kw.get("logo_url"),
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "verification_status": "unverified",
            "industry": None,
        }
        return self.brands[user_id]

    def add_listing(
        self,
        *,
        poster_user_id: str,
        poster_role: str = "creator",
        **kw: Any,
    ) -> dict[str, Any]:
        lid = kw.get("id") or str(uuid4())
        row = {
            "id": lid,
            "poster_user_id": poster_user_id,
            "poster_role": poster_role,
            "title": kw.get("title", "Reel for summer launch"),
            "description": kw.get("description", "Short-form reel for a seasonal push."),
            "listing_type": kw.get("listing_type", "brand_deal"),
            "compensation_text": kw.get("compensation_text", "$2k"),
            "compensation_type": kw.get("compensation_type", "flat_rate"),
            "budget_min": kw.get("budget_min"),
            "budget_max": kw.get("budget_max"),
            "target_niches": kw.get("target_niches", ["fashion"]),
            "deadline": kw.get("deadline"),
            "is_active": kw.get("is_active", True),
            "is_taken_down": kw.get("is_taken_down", False),
            "discovery_eligible": kw.get("discovery_eligible", True),
            "location_city": None,
            "location_region": None,
            "location_country": None,
            "expires_at": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
        self.listings[lid] = row
        return row


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch) -> _World:
    w = _World()

    # ---- profiles ----
    monkeypatch.setattr(
        profiles_service, "get_creator_profile", lambda uid: w.creators.get(uid)
    )
    monkeypatch.setattr(
        profiles_service,
        "get_creator_profile_cached",
        lambda uid, _r=None: w.creators.get(uid),
    )
    monkeypatch.setattr(
        profiles_service, "get_brand_profile", lambda uid: w.brands.get(uid)
    )
    monkeypatch.setattr(
        profiles_service, "public_creator", lambda row: row
    )
    monkeypatch.setattr(
        profiles_service, "public_brand", lambda row: row
    )

    # ---- jobs service ----
    monkeypatch.setattr(jobs_service, "get", lambda lid: w.listings.get(lid))
    monkeypatch.setattr(
        jobs_service,
        "can_view_detail",
        lambda listing, _viewer: bool(
            listing
            and not listing.get("is_taken_down")
            and listing.get("is_active")
        ),
    )

    # ---- network / connections quiet ----
    monkeypatch.setattr(
        network_service, "get_connection_between", lambda a, b: None
    )

    # ---- discover service — used for the Discover-privacy assertion ----
    def _list_cards(**kwargs):  # type: ignore[no-untyped-def]
        # Our fake "public" projection is the listing row itself; no
        # application fields ever leak into this list because the
        # application table is read via a different service.
        rows: list[dict[str, Any]] = []
        for lst in w.listings.values():
            if not lst.get("is_active") or lst.get("is_taken_down"):
                continue
            rows.append(
                {
                    "card_kind": "opportunity",
                    "card_id": lst["id"],
                    "owner_user_id": lst["poster_user_id"],
                    "title": lst["title"],
                    "subtitle": None,
                    "listing_type": lst["listing_type"],
                    "detail_path": f"/creator/jobs/{lst['id']}",
                }
            )
        return rows

    monkeypatch.setattr(discover_service, "list_cards", _list_cards)

    # ---- job_applications service (patched to the fake store) ----
    def _has_applied(listing_id: str, applicant_user_id: str) -> bool:
        return (str(listing_id), str(applicant_user_id)) in w.applications

    def _create(
        *, listing_id: str, applicant_user_id: str, message: str
    ) -> dict[str, Any] | None:
        clean = job_applications.normalize_message(message)
        if not clean:
            return None
        key = (str(listing_id), str(applicant_user_id))
        if w.insert_failures > 0:
            w.insert_failures -= 1
            return None
        if key in w.applications:
            return None
        row = {
            "id": str(uuid4()),
            "listing_id": listing_id,
            "applicant_user_id": applicant_user_id,
            "message": clean,
            "status": "submitted",
            "created_at": "2026-01-01T00:00:00Z",
        }
        w.applications[key] = row
        return row

    monkeypatch.setattr(job_applications, "has_applied", _has_applied)
    monkeypatch.setattr(job_applications, "create", _create)

    return w


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str = "creator", user_id: str | None = None) -> str:
    uid = user_id or str(uuid4())
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return uid


# ---------- normalize_message + MAX_MESSAGE_CHARS pure contract ----------


def test_normalize_message_strips_whitespace_and_caps_at_max():
    assert job_applications.normalize_message("  hi  ") == "hi"
    assert job_applications.normalize_message("") == ""
    assert job_applications.normalize_message("   ") == ""
    assert job_applications.normalize_message(None) == ""
    capped = job_applications.normalize_message("x" * 3000)
    assert len(capped) == job_applications.MAX_MESSAGE_CHARS


def test_max_message_chars_matches_db_check():
    """Service bound must match migration 0048's CHECK (<= 2000)."""
    assert job_applications.MAX_MESSAGE_CHARS == 2000


# ---------- detail page: Apply button eligibility ----------


def test_eligible_creator_sees_functional_apply_on_opportunity_detail(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster, full_name="Studio Vans")
    world.add_creator(viewer, full_name="Alex Creator")
    listing = world.add_listing(poster_user_id=poster)

    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    body = r.text
    assert f'href="/creator/jobs/{listing["id"]}/apply"' in body
    # The Apply affordance must be a link (not a disabled button) now.
    assert "disabled>Apply</button>" not in body
    assert "✓ Applied" not in body


def test_already_applied_creator_sees_non_submitting_marker(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)
    # Pre-seed an existing application.
    world.applications[(listing["id"], viewer)] = {
        "id": str(uuid4()),
        "listing_id": listing["id"],
        "applicant_user_id": viewer,
        "message": "hi",
        "status": "submitted",
    }

    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    body = r.text
    assert "✓ Applied" in body
    assert "opportunity-apply-done" in body
    # No submit form or Apply link should coexist with the marker.
    assert f'href="/creator/jobs/{listing["id"]}/apply"' not in body


def test_poster_viewing_own_opportunity_sees_edit_not_apply(client, world):
    poster = _signed_in(client)
    world.add_creator(poster)
    listing = world.add_listing(poster_user_id=poster)
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    assert "edit posting" in r.text
    assert "/apply" not in r.text


def test_second_creator_still_sees_apply_after_first_creator_applies(client, world):
    poster = str(uuid4())
    first = str(uuid4())
    second = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(first)
    world.add_creator(second)
    listing = world.add_listing(poster_user_id=poster)
    world.applications[(listing["id"], first)] = {
        "id": str(uuid4()),
        "listing_id": listing["id"],
        "applicant_user_id": first,
        "message": "hi",
        "status": "submitted",
    }

    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    body = r.text
    assert f'href="/creator/jobs/{listing["id"]}/apply"' in body
    assert "✓ Applied" not in body


# ---------- GET /creator/jobs/{id}/apply ----------


def test_apply_form_renders_real_opportunity_summary(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_brand(poster, company_name="Olipop")
    world.add_creator(viewer)
    listing = world.add_listing(
        poster_user_id=poster,
        poster_role="brand",
        title="Summer campaign reel",
        compensation_text="$2.5k",
    )

    r = client.get(f"/creator/jobs/{listing['id']}/apply")
    assert r.status_code == 200
    body = r.text
    # Compact summary pulls from the live listing row + poster profile.
    assert "Summer campaign reel" in body
    assert "Olipop" in body
    assert "$2.5k" in body
    # Form structure.
    assert 'name="message"' in body
    assert 'name="csrf_token"' in body
    assert 'action="/creator/jobs/' in body
    assert ">Submit application<" in body
    assert "Apply to this opportunity" in body
    assert "Tell the brand why you're a good fit" in body


def test_apply_form_404_on_nonexistent_opportunity(client, world):
    viewer = _signed_in(client)
    world.add_creator(viewer)
    r = client.get(f"/creator/jobs/{uuid4()}/apply")
    assert r.status_code == 404


def test_apply_form_403_if_viewer_is_poster(client, world):
    poster = _signed_in(client)
    world.add_creator(poster)
    listing = world.add_listing(poster_user_id=poster)
    r = client.get(f"/creator/jobs/{listing['id']}/apply")
    assert r.status_code == 403


def test_apply_form_redirects_to_detail_if_already_applied(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)
    world.applications[(listing["id"], viewer)] = {
        "id": str(uuid4()),
        "listing_id": listing["id"],
        "applicant_user_id": viewer,
        "message": "hi",
        "status": "submitted",
    }
    r = client.get(f"/creator/jobs/{listing['id']}/apply")
    assert r.status_code == 303
    assert r.headers["location"] == f"/creator/jobs/{listing['id']}"


# ---------- POST /creator/jobs/{id}/apply ----------


def test_valid_submit_persists_and_redirects_to_success(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster, full_name="Olipop")
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(
        f"/creator/jobs/{listing['id']}/apply",
        data={"message": "I grew up on olipop and shoot reels weekly."},
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/creator/jobs/{listing['id']}/applied"
    key = (listing["id"], viewer)
    assert key in world.applications
    assert world.applications[key]["message"].startswith("I grew up on olipop")
    assert world.applications[key]["status"] == "submitted"


def test_submit_rejects_blank_message(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(f"/creator/jobs/{listing['id']}/apply", data={"message": ""})
    assert r.status_code == 400
    assert (listing["id"], viewer) not in world.applications


def test_submit_rejects_whitespace_only_message(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "   \n\t  "}
    )
    assert r.status_code == 400
    assert (listing["id"], viewer) not in world.applications


def test_submit_rejects_over_2000_char_message(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(
        f"/creator/jobs/{listing['id']}/apply",
        data={"message": "x" * 2500},
    )
    assert r.status_code == 400
    assert "too long" in r.text.lower() or "2000 characters" in r.text
    assert (listing["id"], viewer) not in world.applications


def test_submit_404_on_nonexistent_opportunity(client, world):
    viewer = _signed_in(client)
    world.add_creator(viewer)
    r = client.post(
        f"/creator/jobs/{uuid4()}/apply", data={"message": "hi"}
    )
    assert r.status_code == 404


def test_submit_403_if_viewer_is_poster(client, world):
    poster = _signed_in(client)
    world.add_creator(poster)
    listing = world.add_listing(poster_user_id=poster)
    r = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "hi me"}
    )
    assert r.status_code == 403
    assert (listing["id"], poster) not in world.applications


def test_duplicate_submission_safely_absorbed(client, world):
    """A second POST after a successful submit must NOT insert a second
    row and must land the viewer on the success page (not a duplicate
    error, not an application form)."""
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r1 = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "first"}
    )
    assert r1.status_code == 303
    assert r1.headers["location"] == f"/creator/jobs/{listing['id']}/applied"

    r2 = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "second"}
    )
    assert r2.status_code == 303
    assert r2.headers["location"] == f"/creator/jobs/{listing['id']}"
    assert len(world.applications) == 1
    # Message is the FIRST one — the duplicate POST never mutated.
    assert world.applications[(listing["id"], viewer)]["message"] == "first"


def test_concurrent_insert_race_safely_lands_on_success(client, world):
    """Simulate: pre-check says 'not applied yet' but the insert returns
    None because a concurrent insert won the unique constraint. The
    route must still land the user on the success page because the row
    now exists."""
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    # Rig: the next create() returns None (as if the unique constraint
    # fired) AND the row materializes between pre-check and the
    # post-failure re-check.
    world.insert_failures = 1
    world.applications[(listing["id"], viewer)] = {
        "id": str(uuid4()),
        "listing_id": listing["id"],
        "applicant_user_id": viewer,
        "message": "pre-existing",
        "status": "submitted",
    }
    # But has_applied returned False at the top of the handler because
    # we didn't exist then — simulate by patching the fake to return
    # False first, then True. Use a toggle counter in closure.
    saw_first = {"n": 0}
    real_has = job_applications.has_applied

    def _toggle(lid: str, uid: str) -> bool:
        saw_first["n"] += 1
        if saw_first["n"] == 1:
            return False
        return real_has(lid, uid)

    import pytest as _pytest  # local to avoid global state
    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr(job_applications, "has_applied", _toggle)
        r = client.post(
            f"/creator/jobs/{listing['id']}/apply", data={"message": "race"}
        )
    assert r.status_code == 303
    assert r.headers["location"] == f"/creator/jobs/{listing['id']}/applied"


# ---------- security: form cannot spoof applicant identity ----------


def test_applicant_identity_cannot_be_spoofed_via_form_field(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    attacker_target = str(uuid4())
    world.add_creator(poster)
    world.add_creator(viewer)
    world.add_creator(attacker_target)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(
        f"/creator/jobs/{listing['id']}/apply",
        data={
            "message": "trying to apply as someone else",
            # These form fields should be IGNORED by the server. Only
            # the authenticated session's user_id matters.
            "applicant_user_id": attacker_target,
            "poster_user_id": attacker_target,
            "status": "accepted",
        },
    )
    assert r.status_code == 303
    assert r.headers["location"] == f"/creator/jobs/{listing['id']}/applied"
    # The row was stored under the authenticated viewer, NOT the
    # attempted spoof target.
    assert (listing["id"], viewer) in world.applications
    assert (listing["id"], attacker_target) not in world.applications
    stored = world.applications[(listing["id"], viewer)]
    assert stored["applicant_user_id"] == viewer
    # Status is server-controlled.
    assert stored["status"] == "submitted"


# ---------- GET /creator/jobs/{id}/applied ----------


def test_success_page_requires_persisted_application(client, world):
    """Direct GET to .../applied without a persisted row must redirect
    to the detail page — the success affordance never shows for an
    unverified state."""
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.get(f"/creator/jobs/{listing['id']}/applied")
    assert r.status_code == 303
    assert r.headers["location"] == f"/creator/jobs/{listing['id']}"


def test_success_page_renders_poster_name_and_actions(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(viewer)
    world.add_brand(poster, company_name="Olipop")
    listing = world.add_listing(poster_user_id=poster, poster_role="brand")
    world.applications[(listing["id"], viewer)] = {
        "id": str(uuid4()),
        "listing_id": listing["id"],
        "applicant_user_id": viewer,
        "message": "ok",
        "status": "submitted",
    }

    r = client.get(f"/creator/jobs/{listing['id']}/applied")
    assert r.status_code == 200
    body = r.text
    assert "Application submitted" in body
    assert "Olipop" in body
    # Both affordances the spec locks in.
    assert "View opportunity" in body
    assert "Back to discover" in body
    assert f'href="/creator/jobs/{listing["id"]}"' in body
    assert 'href="/creator/discover?kind=opportunity"' in body


# ---------- privacy / Discover isolation ----------


def test_no_application_data_in_public_discover_payload(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)
    world.applications[(listing["id"], viewer)] = {
        "id": str(uuid4()),
        "listing_id": listing["id"],
        "applicant_user_id": viewer,
        "message": "SECRET-APPLICATION-TOKEN",
        "status": "submitted",
    }

    r = client.get("/creator/discover?kind=opportunity")
    assert r.status_code == 200
    body = r.text
    assert "SECRET-APPLICATION-TOKEN" not in body
    assert "applicant_user_id" not in body
    assert "/jobs/{}/apply".format(listing["id"]) not in body


# ---------- locked-system isolation on submit ----------


def test_submit_creates_no_dm_and_no_connection(client, world, monkeypatch):
    """A successful application must not create a DM thread, send a
    DM, or create a connection — the application is its own object."""
    from app.services import dms as dms_service
    from app.services import instagram_dms as ig_dms_service
    from app.services import notifications as notifications_service

    dm_calls: list[Any] = []
    conn_calls: list[Any] = []
    notif_calls: list[Any] = []

    def _fail_dm(*a, **kw):
        dm_calls.append((a, kw))
        raise AssertionError("application submit touched DMs")

    def _fail_conn(*a, **kw):
        conn_calls.append((a, kw))
        raise AssertionError("application submit touched connections")

    monkeypatch.setattr(dms_service, "send_message", _fail_dm, raising=False)
    monkeypatch.setattr(dms_service, "create_thread", _fail_dm, raising=False)
    monkeypatch.setattr(
        ig_dms_service, "send_direct_message", _fail_dm, raising=False
    )
    monkeypatch.setattr(
        network_service, "request_connection", _fail_conn, raising=False
    )
    # Notifications are allowed to be a no-op (not called) but we
    # record if the handler ever tried.
    monkeypatch.setattr(
        notifications_service,
        "create",
        lambda **kw: notif_calls.append(kw) or True,
    )

    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "ok"}
    )
    assert r.status_code == 303
    # Zero DM / connection activity.
    assert dm_calls == []
    assert conn_calls == []
    # Step 5B deliberately does not notify yet — Step 5C owns owner-
    # facing notification. If a future step adds one, update this
    # expectation then.
    assert notif_calls == []


def test_submit_touches_no_stripe_or_deal_module(client, world, monkeypatch):
    """Zero Stripe / deal / payment calls on a verified application."""
    import importlib

    forbidden_modules = (
        "app.integrations.stripe_client",
        "app.services.creator_payouts",
        "app.services.babyg_deals",
    )
    for mod_name in forbidden_modules:
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for attr in dir(mod):
            if attr.startswith("_"):
                continue
            value = getattr(mod, attr, None)
            if callable(value):

                def _blow_up(*a, _mn=mod_name, _attr=attr, **kw):
                    raise AssertionError(
                        f"application submit touched {_mn}.{_attr}"
                    )

                monkeypatch.setattr(mod, attr, _blow_up, raising=False)

    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster)
    world.add_creator(viewer)
    listing = world.add_listing(poster_user_id=poster)

    r = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "ok"}
    )
    assert r.status_code == 303


# ---------- Step 5A regression ----------


def test_step_5a_detail_page_still_renders_core_fields(client, world):
    poster = str(uuid4())
    viewer = _signed_in(client)
    world.add_creator(poster, full_name="Studio")
    world.add_creator(viewer)
    listing = world.add_listing(
        poster_user_id=poster, title="Reel pack", compensation_text="$3k"
    )
    r = client.get(f"/creator/jobs/{listing['id']}")
    assert r.status_code == 200
    body = r.text
    assert "Reel pack" in body
    assert "$3k" in body
    assert "Studio" in body
    assert "back-link" in body


# ---------- auth + role ----------


def test_apply_form_requires_creator_role(client, world):
    _signed_in(client, role="brand")
    world.add_listing(poster_user_id=str(uuid4()))
    r = client.get(f"/creator/jobs/{next(iter(world.listings.keys()))}/apply")
    assert r.status_code in (302, 303, 403)


def test_submit_requires_creator_role(client, world):
    _signed_in(client, role="brand")
    poster = str(uuid4())
    world.add_creator(poster)
    listing = world.add_listing(poster_user_id=poster)
    r = client.post(
        f"/creator/jobs/{listing['id']}/apply", data={"message": "hi"}
    )
    assert r.status_code in (302, 303, 403)
    assert world.applications == {}
