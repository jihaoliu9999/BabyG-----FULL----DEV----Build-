"""Step 5D: applicant review for the poster of an opportunity.

Flow under test:

    Discover -> Opportunities -> My opportunities -> (a POSTED opportunity)
      -> Applicants -> (one applicant) -> Application -> "View profile"

Access is decided by the viewer's RELATIONSHIP to the opportunity, never by
anything the client sends: the listing's ``poster_user_id`` must equal the
authenticated session user. These tests run the REAL ``jobs``, ``profiles``
and ``job_applications`` services against an in-memory PostgREST fake that
honors ``eq`` / ``in_`` / ordering / column projection, so they prove the
actual queries (columns selected, filters applied) and not just the routes.
Any table the fake does not know fails like the unconfigured production
client does, exactly as in the other route tests.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import discovery, job_applications, network, views
from app.services import profiles as profiles_service

SECRET = "SECRET-APPLICATION-TEXT-9f3a"
WHEN = "2026-03-04T10:30:00Z"

_TABLES = (
    "creator_job_listings",
    "creator_job_applications",
    "creator_profiles",
    "brand_profiles",
    "creator_job_offers",  # read by the Application page since Step 6A
)


# --------------------------------------------------------- in-memory PostgREST


class _Query:
    def __init__(self, db: _FakeDB, name: str) -> None:
        self.db, self.name = db, name
        self.op = "select"
        self.cols: list[str] | None = None
        self.eqs: list[tuple[str, Any]] = []
        self.ins: list[tuple[str, list[Any]]] = []
        self.orders: list[tuple[str, bool]] = []
        self.lim: int | None = None
        self.rng: tuple[int, int] | None = None
        self.body: dict[str, Any] | None = None

    def select(self, cols: str = "*") -> _Query:
        self.op = "select"
        self.cols = None if cols.strip() == "*" else [c.strip() for c in cols.split(",")]
        return self

    def insert(self, body: dict[str, Any]) -> _Query:
        self.op, self.body = "insert", dict(body)
        return self

    def eq(self, col: str, val: Any) -> _Query:
        self.eqs.append((col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> _Query:
        self.ins.append((col, list(vals)))
        return self

    def order(self, col: str, desc: bool = False) -> _Query:
        self.orders.append((col, desc))
        return self

    def limit(self, n: int) -> _Query:
        self.lim = n
        return self

    def range(self, lo: int, hi: int) -> _Query:
        self.rng = (lo, hi)
        return self

    def execute(self) -> SimpleNamespace:
        self.db.queries.append(
            {
                "table": self.name,
                "op": self.op,
                "cols": self.cols,
                "eqs": list(self.eqs),
                "ins": list(self.ins),
            }
        )
        if self.name in self.db.fail_tables:
            raise PostgrestAPIError({"message": "boom", "code": "500", "hint": None, "details": None})
        rows = self.db.tables[self.name]
        if self.op == "insert":
            assert self.body is not None
            if self.name == "creator_job_applications" and any(
                r["listing_id"] == self.body["listing_id"]
                and r["applicant_user_id"] == self.body["applicant_user_id"]
                for r in rows
            ):
                raise PostgrestAPIError(
                    {"message": "duplicate", "code": "23505", "hint": None, "details": None}
                )
            row = {"id": str(uuid4()), "status": "submitted", "created_at": WHEN, **self.body}
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        out = [
            r
            for r in rows
            if all(str(r.get(c)) == str(v) for c, v in self.eqs)
            and all(str(r.get(c)) in {str(x) for x in vs} for c, vs in self.ins)
        ]
        for col, desc in reversed(self.orders):
            out.sort(key=lambda r, c=col: str(r.get(c) or ""), reverse=desc)
        if self.rng:
            out = out[self.rng[0] : self.rng[1] + 1]
        if self.lim is not None:
            out = out[: self.lim]
        if self.cols is not None:
            out = [{c: r.get(c) for c in self.cols} for r in out]
        return SimpleNamespace(data=[dict(r) for r in out])


class _FakeDB:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {t: [] for t in _TABLES}
        self.queries: list[dict[str, Any]] = []
        self.fail_tables: set[str] = set()

    def table(self, name: str) -> _Query:
        if name not in self.tables:
            raise RuntimeError("supabase env missing (test fake)")
        return _Query(self, name)

    def app_queries(self) -> list[dict[str, Any]]:
        return [q for q in self.queries if q["table"] == "creator_job_applications"]


class _World:
    def __init__(self, db: _FakeDB) -> None:
        self.db = db
        self.discoverable: set[str] = set()

    def creator(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        row = {
            "user_id": uid,
            "full_name": kw.pop("full_name", "Sam Rivera"),
            "instagram_handle": kw.pop("instagram_handle", "samrivera"),
            "profile_photo_url": kw.pop("profile_photo_url", None),
            "onboarding_completed_at": kw.pop("onboarding_completed_at", "2026-01-01T00:00:00Z"),
            "niches": ["fashion"],
            "content_formats": [],
            "hard_limits": [],
            "primary_platform": "Instagram",
            "bio": "Short bio.",
            "SECRET_PRIVATE_FIELD": "must-never-render",
        }
        row.update(kw)
        self.db.tables["creator_profiles"].append(row)
        return uid

    def brand(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        row = {
            "user_id": uid,
            "company_name": kw.pop("company_name", "Olipop"),
            "onboarding_completed_at": kw.pop("onboarding_completed_at", "2026-01-01T00:00:00Z"),
            "location_city": "Austin",
            "location_region": "TX",
            "niche_preferences": [],
        }
        row.update(kw)
        self.db.tables["brand_profiles"].append(row)
        return uid

    def listing(self, poster: str, **kw: Any) -> dict[str, Any]:
        row = {
            "id": kw.pop("id", None) or str(uuid4()),
            "poster_user_id": poster,
            "poster_role": kw.pop("poster_role", "brand"),
            "title": kw.pop("title", "Summer reel pack"),
            "description": "Four short-form reels.",
            "listing_type": "brand_deal",
            "compensation_text": "$2k",
            "budget_min": None,
            "budget_max": None,
            "target_niches": ["fashion"],
            "deadline": None,
            "is_active": kw.pop("is_active", True),
            "is_taken_down": kw.pop("is_taken_down", False),
            "discovery_eligible": True,
            "expires_at": None,
            "location_city": None,
            "location_region": None,
            "created_at": "2026-01-01T00:00:00Z",
        }
        row.update(kw)
        self.db.tables["creator_job_listings"].append(row)
        return row

    def apply(
        self, listing_id: str, applicant: str, message: str = SECRET, when: str = WHEN
    ) -> str:
        aid = str(uuid4())
        self.db.tables["creator_job_applications"].append(
            {
                "id": aid,
                "listing_id": listing_id,
                "applicant_user_id": applicant,
                "message": message,
                "status": "submitted",
                "created_at": when,
            }
        )
        return aid


@pytest.fixture()
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> _World:
    w = _World(db)
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: db)

    # Collaborators that are not under test (no real network, no side effects)
    monkeypatch.setattr(network, "get_connection_between", lambda a, b: None)
    monkeypatch.setattr(views, "record_view", lambda **kw: True)
    monkeypatch.setattr(discovery, "record_action", lambda **kw: True)
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(discover_service, "record_action", lambda **kw: True)

    def _creator_card(uid: str) -> dict[str, Any] | None:
        if uid not in w.discoverable:
            return None
        return {
            "card_kind": "creator",
            "card_id": uid,
            "owner_user_id": uid,
            "title": "Sam Rivera",
            "subtitle": "@samrivera",
            "image_url": None,
            "location_label": None,
            "tags": [],
            "description": "Short bio.",
            "relevance_reasons": [],
            "detail_path": f"/creator/network/{uid}",
        }

    monkeypatch.setattr(
        discover_service,
        "get_card",
        lambda *, card_kind, card_id, **kw: _creator_card(card_id) if card_kind == "creator" else None,
    )

    def _opportunity_cards(ids: list[str]) -> dict[str, dict[str, Any]]:
        out = {}
        for r in db.tables["creator_job_listings"]:
            if r["id"] in ids and r["is_active"] and not r["is_taken_down"]:
                out[r["id"]] = {
                    "card_kind": "opportunity",
                    "card_id": r["id"],
                    "owner_user_id": r["poster_user_id"],
                    "title": r["title"],
                    "subtitle": "Olipop",
                    "location_label": "Austin, TX",
                    "tags": ["fashion"],
                    "description": r["description"],
                    "compensation_text": r["compensation_text"],
                    "budget_min": None,
                    "budget_max": None,
                    "deadline": None,
                    "detail_path": f"/creator/jobs/{r['id']}",
                }
        return out

    monkeypatch.setattr(discover_service, "get_opportunity_cards", _opportunity_cards)
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [])
    return w


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, uid: str, role: str = "creator") -> str:
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])
    return uid


def _row_links(html: str) -> list[str]:
    return re.findall(r'<a class="opportunity-applicant-row" href="([^"]*)"', html)


def _card_links(html: str) -> list[str]:
    return re.findall(r'<a class="discover-card-hit" href="([^"]*)"', html)


def _brand_poster(client, world) -> tuple[str, dict[str, Any]]:
    me = world.brand()
    _sign_in(client, me, "brand")
    return me, world.listing(me, title="Autumn campaign reels")


def _creator_poster(client, world) -> tuple[str, dict[str, Any]]:
    me = world.creator(full_name="Poster Creator")
    _sign_in(client, me, "creator")
    return me, world.listing(me, poster_role="creator", title="Collab wanted")


def _list_url(role: str, lid: str) -> str:
    return (
        f"/brand/discover/opportunity/{lid}/applicants"
        if role == "brand"
        else f"/creator/jobs/{lid}/applicants"
    )


def _poster(client, world, role: str) -> tuple[str, dict[str, Any]]:
    return _brand_poster(client, world) if role == "brand" else _creator_poster(client, world)


ROLES = pytest.mark.parametrize("role", ["brand", "creator"])


# =============================================== service: authorize_poster


def test_authorize_poster_ok_only_for_the_poster(world):
    owner = world.brand()
    lst = world.listing(owner)
    status, row = job_applications.authorize_poster(lst["id"], owner)
    assert status == job_applications.REVIEW_OK
    assert row is not None and row["id"] == lst["id"]


def test_authorize_poster_forbidden_for_a_visible_listing_of_someone_else(world):
    owner, other = world.brand(), world.creator()
    lst = world.listing(owner)
    status, row = job_applications.authorize_poster(lst["id"], other)
    assert status == job_applications.REVIEW_FORBIDDEN
    assert row is None  # the listing row is never handed to a non-owner


@pytest.mark.parametrize("bad", ["", "not-a-uuid", "1' or '1'='1", "../../etc/passwd", "x" * 64])
def test_authorize_poster_malformed_listing_id_is_not_found(world, db, bad):
    viewer = world.creator()
    before = len(db.queries)
    assert job_applications.authorize_poster(bad, viewer) == (job_applications.REVIEW_NOT_FOUND, None)
    assert len(db.queries) == before  # never even queried


def test_authorize_poster_malformed_viewer_id_is_not_found(world):
    lst = world.listing(world.brand())
    assert job_applications.authorize_poster(lst["id"], "nope")[0] == job_applications.REVIEW_NOT_FOUND
    assert job_applications.authorize_poster(lst["id"], "")[0] == job_applications.REVIEW_NOT_FOUND


def test_authorize_poster_missing_listing_is_not_found(world):
    assert job_applications.authorize_poster(str(uuid4()), str(uuid4()))[0] == (
        job_applications.REVIEW_NOT_FOUND
    )


def test_authorize_poster_taken_down_listing_is_not_found_even_for_the_owner(world):
    owner = world.brand()
    lst = world.listing(owner, is_taken_down=True)
    assert job_applications.authorize_poster(lst["id"], owner)[0] == job_applications.REVIEW_NOT_FOUND


def test_authorize_poster_closed_listing_stays_reviewable_by_its_owner(world):
    owner = world.brand()
    lst = world.listing(owner, is_active=False)
    assert job_applications.authorize_poster(lst["id"], owner)[0] == job_applications.REVIEW_OK


def test_authorize_poster_closed_listing_is_hidden_from_everyone_else(world):
    lst = world.listing(world.brand(), is_active=False)
    # not publicly viewable -> indistinguishable from a missing listing
    assert job_applications.authorize_poster(lst["id"], world.creator())[0] == (
        job_applications.REVIEW_NOT_FOUND
    )


# ===================================================== service: list_for_poster


def test_list_for_poster_selects_no_message_and_pins_the_listing(world, db):
    owner = world.brand()
    lst = world.listing(owner)
    other = world.listing(owner, title="Other")
    a = world.creator()
    world.apply(lst["id"], a)
    world.apply(other["id"], world.creator())
    rows = job_applications.list_for_poster(lst, owner)
    assert rows is not None and len(rows) == 1
    q = db.app_queries()[-1]
    assert q["cols"] == ["id", "applicant_user_id", "created_at"]
    assert "message" not in q["cols"]
    assert ("listing_id", lst["id"]) in q["eqs"]
    assert set(rows[0]) == {"id", "applicant_user_id", "created_at"}
    assert rows[0]["applicant_user_id"] == a


def test_list_for_poster_never_returns_message_even_if_a_row_carried_one(world, monkeypatch):
    owner = world.brand()
    lst = world.listing(owner)
    world.apply(lst["id"], world.creator())

    class _Leaky:
        def table(self, name):
            return self

        def __getattr__(self, n):
            return lambda *a, **k: self

        def execute(self):
            return SimpleNamespace(
                data=[{"id": "i", "applicant_user_id": "u", "created_at": "c", "message": SECRET}]
            )

    monkeypatch.setattr(supabase_client, "get_service_client", lambda: _Leaky())
    rows = job_applications.list_for_poster(lst, owner)
    assert rows and "message" not in rows[0]
    assert SECRET not in repr(rows)


def test_list_for_poster_refuses_non_owner_without_querying(world, db):
    lst = world.listing(world.brand())
    world.apply(lst["id"], world.creator())
    before = len(db.app_queries())
    assert job_applications.list_for_poster(lst, world.creator()) is None
    assert job_applications.list_for_poster(lst, "") is None
    assert len(db.app_queries()) == before


def test_list_for_poster_newest_first(world):
    owner = world.brand()
    lst = world.listing(owner)
    old, new = world.creator(), world.creator()
    world.apply(lst["id"], old, when="2026-01-01T00:00:00Z")
    world.apply(lst["id"], new, when="2026-02-01T00:00:00Z")
    rows = job_applications.list_for_poster(lst, owner)
    assert [r["applicant_user_id"] for r in rows] == [new, old]


def test_list_for_poster_zero_is_empty_list_but_failure_is_none(world, db):
    owner = world.brand()
    lst = world.listing(owner)
    assert job_applications.list_for_poster(lst, owner) == []
    db.fail_tables.add("creator_job_applications")
    assert job_applications.list_for_poster(lst, owner) is None  # NOT [] ("no applicants")


def test_list_for_poster_is_capped(world):
    owner = world.brand()
    lst = world.listing(owner)
    for i in range(205):
        world.apply(lst["id"], str(uuid4()), when=f"2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z")
    assert len(job_applications.list_for_poster(lst, owner)) == 200


# ======================================================= service: get_for_poster


def test_get_for_poster_returns_the_exact_stored_message(world):
    owner = world.brand()
    lst = world.listing(owner)
    msg = "Line one.\nLine two with <b>tags</b> & ampersand “quotes”."
    a = world.creator()
    aid = world.apply(lst["id"], a, message=msg)
    row = job_applications.get_for_poster(lst, aid, owner)
    assert row is not None
    assert row["message"] == msg
    assert row["applicant_user_id"] == a  # from the stored row, not a parameter
    assert row["listing_id"] == lst["id"]


def test_get_for_poster_query_is_pinned_to_application_and_listing(world, db):
    owner = world.brand()
    lst = world.listing(owner)
    aid = world.apply(lst["id"], world.creator())
    job_applications.get_for_poster(lst, aid, owner)
    q = db.app_queries()[-1]
    assert ("id", aid) in q["eqs"] and ("listing_id", lst["id"]) in q["eqs"]


def test_get_for_poster_application_from_another_listing_is_none(world):
    owner = world.brand()
    mine, theirs = world.listing(owner), world.listing(owner, title="Second")
    aid_other = world.apply(theirs["id"], world.creator())
    assert job_applications.get_for_poster(mine, aid_other, owner) is None


def test_get_for_poster_application_from_a_listing_i_do_not_own_is_none(world):
    owner, stranger = world.brand(), world.brand()
    mine = world.listing(owner)
    foreign = world.listing(stranger)
    aid_foreign = world.apply(foreign["id"], world.creator())
    assert job_applications.get_for_poster(mine, aid_foreign, owner) is None


def test_get_for_poster_refuses_non_owner_without_querying(world, db):
    owner = world.brand()
    lst = world.listing(owner)
    aid = world.apply(lst["id"], world.creator())
    before = len(db.app_queries())
    assert job_applications.get_for_poster(lst, aid, world.creator()) is None
    assert len(db.app_queries()) == before


@pytest.mark.parametrize("bad", ["", "nope", "1;drop table x", "00000000-0000-0000-0000-00000000000"])
def test_get_for_poster_malformed_application_id_is_none(world, db, bad):
    owner = world.brand()
    lst = world.listing(owner)
    before = len(db.app_queries())
    assert job_applications.get_for_poster(lst, bad, owner) is None
    assert len(db.app_queries()) == before


def test_get_for_poster_missing_and_failing_reads_are_none(world, db):
    owner = world.brand()
    lst = world.listing(owner)
    assert job_applications.get_for_poster(lst, str(uuid4()), owner) is None
    aid = world.apply(lst["id"], world.creator())
    db.fail_tables.add("creator_job_applications")
    assert job_applications.get_for_poster(lst, aid, owner) is None


def test_get_for_poster_rechecks_the_returned_row_against_the_listing(world, monkeypatch):
    owner = world.brand()
    lst = world.listing(owner)
    aid = str(uuid4())

    class _Wrong:
        def table(self, name):
            return self

        def __getattr__(self, n):
            return lambda *a, **k: self

        def execute(self):
            # a row from a DIFFERENT listing slips past (hypothetical filter bug)
            return SimpleNamespace(
                data=[
                    {
                        "id": aid,
                        "listing_id": str(uuid4()),
                        "applicant_user_id": str(uuid4()),
                        "message": SECRET,
                        "created_at": WHEN,
                    }
                ]
            )

    monkeypatch.setattr(supabase_client, "get_service_client", lambda: _Wrong())
    assert job_applications.get_for_poster(lst, aid, owner) is None


# ======================================================= service: identity


def test_applicant_identity_uses_only_existing_profile_fields():
    ident = job_applications.applicant_identity(
        {
            "full_name": "Sam Rivera",
            "instagram_handle": "@samrivera",
            "profile_photo_url": "https://cdn.example/p.jpg",
            "onboarding_completed_at": "x",
            "follower_range": "100k+",
        },
        "u1",
    )
    assert ident["name"] == "Sam Rivera"
    assert ident["descriptor"] == "@samrivera"
    assert ident["image_url"] == "https://cdn.example/p.jpg"
    assert ident["initial"] == "S"
    assert ident["found"] and ident["onboarded"]
    assert "100k" not in repr(ident)  # nothing invented / surfaced beyond the descriptor


def test_applicant_identity_handle_only_has_no_duplicate_descriptor():
    ident = job_applications.applicant_identity({"instagram_handle": "samrivera"}, "u1")
    assert ident["name"] == "@samrivera"
    assert ident["descriptor"] == ""
    assert ident["initial"] == "S"


def test_applicant_identity_missing_profile_is_neutral_not_a_crash():
    ident = job_applications.applicant_identity(None, "u1")
    assert ident["name"] == "creator"
    assert ident["descriptor"] == "" and ident["image_url"] == ""
    assert ident["found"] is False and ident["onboarded"] is False
    assert job_applications.applicant_identity({}, "u1")["name"] == "creator"


def test_attach_applicants_resolves_identity_from_the_stored_user_id(world):
    a, b = world.creator(full_name="Alpha"), world.creator(full_name="Beta")
    rows = [
        {"id": "1", "applicant_user_id": a, "created_at": WHEN},
        {"id": "2", "applicant_user_id": b, "created_at": WHEN},
    ]
    out = job_applications.attach_applicants(rows)
    assert [r["applicant"]["name"] for r in out] == ["Alpha", "Beta"]


def test_attach_applicants_deleted_profile_still_yields_a_row(world):
    ghost = str(uuid4())
    out = job_applications.attach_applicants(
        [{"id": "1", "applicant_user_id": ghost, "created_at": WHEN}]
    )
    assert out[0]["applicant"]["name"] == "creator"
    assert out[0]["applicant"]["found"] is False


def test_attach_applicants_chunks_profile_lookups(world, monkeypatch):
    calls: list[int] = []

    def _spy(ids):
        calls.append(len(ids))
        return {}

    monkeypatch.setattr(profiles_service, "get_creators_by_ids", _spy)
    rows = [{"id": str(i), "applicant_user_id": str(uuid4()), "created_at": WHEN} for i in range(120)]
    job_applications.attach_applicants(rows)
    assert calls == [50, 50, 20]


def test_profiles_are_read_through_the_public_projection(world, client):
    me, lst = _brand_poster(client, world)
    a = world.creator()
    aid = world.apply(lst["id"], a)
    html = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}").text
    assert "must-never-render" not in html


# ================================================ routes: poster, both roles


@ROLES
def test_poster_can_open_their_applicants_list(client, world, role):
    me, lst = _poster(client, world, role)
    a = world.creator(full_name="Alex Kim", instagram_handle="alexk")
    aid = world.apply(lst["id"], a)
    r = client.get(_list_url(role, lst["id"]))
    assert r.status_code == 200
    assert "<h1 class=\"detail-title\">Applicants</h1>" in r.text
    assert lst["title"] in r.text
    assert "Alex Kim" in r.text and "@alexk" in r.text
    assert _row_links(r.text) == [f"{_list_url(role, lst['id'])}/{aid}"]
    assert "mar 4, 2026" in r.text  # submitted date
    assert "opportunity-applicant-chevron" in r.text


@ROLES
def test_list_contains_only_this_opportunitys_applications(client, world, role):
    me, lst = _poster(client, world, role)
    other = world.listing(me, title="A different campaign")
    mine_a = world.creator(full_name="Included Person")
    elsewhere = world.creator(full_name="Excluded Person")
    aid = world.apply(lst["id"], mine_a)
    world.apply(other["id"], elsewhere)
    r = client.get(_list_url(role, lst["id"]))
    assert "Included Person" in r.text
    assert "Excluded Person" not in r.text
    assert len(_row_links(r.text)) == 1 and aid in _row_links(r.text)[0]


@ROLES
def test_list_never_exposes_application_messages(client, world, role):
    me, lst = _poster(client, world, role)
    world.apply(lst["id"], world.creator(), message=SECRET)
    r = client.get(_list_url(role, lst["id"]))
    assert r.status_code == 200
    assert SECRET not in r.text


@ROLES
def test_applicant_identity_is_resolved_from_the_stored_applicant_user_id(client, world, role):
    me, lst = _poster(client, world, role)
    real = world.creator(full_name="Stored Applicant")
    decoy = world.creator(full_name="Decoy Person")
    aid = world.apply(lst["id"], real)
    # the detail URL carries only the application id; supplying someone
    # else's user id anywhere must not change who is shown
    url = f"{_list_url(role, lst['id'])}/{aid}"
    r = client.get(url, params={"applicant_user_id": decoy, "user_id": decoy, "applicant": decoy})
    assert r.status_code == 200
    assert "Stored Applicant" in r.text
    assert "Decoy Person" not in r.text


@ROLES
def test_poster_can_open_one_application_with_the_exact_message(client, world, role):
    me, lst = _poster(client, world, role)
    a = world.creator(full_name="Alex Kim", instagram_handle="alexk")
    msg = "I shoot weekly reels.\n\nHappy to send a rate card — thanks!"
    aid = world.apply(lst["id"], a, message=msg)
    r = client.get(f"{_list_url(role, lst['id'])}/{aid}")
    assert r.status_code == 200
    assert '<h1 class="detail-title">Application</h1>' in r.text
    assert "Alex Kim" in r.text and "@alexk" in r.text
    assert "I shoot weekly reels.\n\nHappy to send a rate card — thanks!" in r.text
    assert "submitted mar 4, 2026" in r.text
    assert lst["title"] in r.text
    assert f'<a href="{_list_url(role, lst["id"])}" class="back-link">← applicants</a>' in r.text


@ROLES
def test_message_is_html_escaped(client, world, role):
    me, lst = _poster(client, world, role)
    aid = world.apply(
        lst["id"], world.creator(full_name="<i>Eve</i>"), message="<script>alert(1)</script>"
    )
    r = client.get(f"{_list_url(role, lst['id'])}/{aid}")
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    assert "<i>Eve</i>" not in r.text


def test_creator_view_profile_points_to_the_existing_creator_profile_and_opens(client, world):
    me, lst = _creator_poster(client, world)
    a = world.creator(full_name="Alex Kim")
    aid = world.apply(lst["id"], a)
    r = client.get(f"/creator/jobs/{lst['id']}/applicants/{aid}")
    assert 'class="btn btn-ghost">View profile</a>' in r.text
    assert f'href="/creator/network/{a}"' in r.text
    page = client.get(f"/creator/network/{a}")
    assert page.status_code == 200
    assert "Alex Kim" in page.text


def test_brand_view_profile_points_to_the_existing_brand_side_profile_and_opens(client, world):
    me, lst = _brand_poster(client, world)
    a = world.creator(full_name="Alex Kim")
    world.discoverable.add(a)
    aid = world.apply(lst["id"], a)
    r = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}")
    assert 'class="btn btn-ghost">View profile</a>' in r.text
    assert f'href="/brand/discover/creator/{a}"' in r.text
    page = client.get(f"/brand/discover/creator/{a}")
    assert page.status_code == 200


def test_brand_view_profile_is_omitted_when_that_route_would_404(client, world):
    me, lst = _brand_poster(client, world)
    a = world.creator()  # not discoverable -> /brand/discover/creator/<a> 404s
    aid = world.apply(lst["id"], a)
    r = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}")
    assert r.status_code == 200
    assert "View profile" not in r.text
    assert client.get(f"/brand/discover/creator/{a}").status_code == 404


@ROLES
def test_view_profile_is_omitted_for_a_missing_or_unfinished_profile(client, world, role):
    me, lst = _poster(client, world, role)
    ghost = str(uuid4())
    unfinished = world.creator(onboarding_completed_at=None, full_name="Half Done")
    world.discoverable.update({ghost, unfinished})
    a1 = world.apply(lst["id"], ghost)
    a2 = world.apply(lst["id"], unfinished)
    for aid in (a1, a2):
        r = client.get(f"{_list_url(role, lst['id'])}/{aid}")
        assert r.status_code == 200
        assert "View profile" not in r.text


@ROLES
def test_zero_applications_renders_the_empty_state(client, world, role):
    me, lst = _poster(client, world, role)
    r = client.get(_list_url(role, lst["id"]))
    assert r.status_code == 200
    assert "No applicants yet" in r.text
    assert "Applications to this opportunity will appear here." in r.text
    assert _row_links(r.text) == []


@ROLES
def test_missing_optional_profile_data_does_not_crash(client, world, role):
    me, lst = _poster(client, world, role)
    bare = world.creator(
        full_name=None, instagram_handle=None, profile_photo_url=None, bio=None, niches=None
    )
    ghost = str(uuid4())  # no profile row at all
    a1 = world.apply(lst["id"], bare, when="2026-03-05T10:00:00Z")
    a2 = world.apply(lst["id"], ghost, when="2026-03-04T10:00:00Z")
    r = client.get(_list_url(role, lst["id"]))
    assert r.status_code == 200
    assert r.text.count("<strong>creator</strong>") == 2
    for aid in (a1, a2):
        d = client.get(f"{_list_url(role, lst['id'])}/{aid}")
        assert d.status_code == 200
        assert SECRET in d.text


@ROLES
def test_closed_listing_is_still_reviewable_by_its_poster(client, world, role):
    me, lst = _poster(client, world, role)
    world.db.tables["creator_job_listings"][-1]["is_active"] = False
    aid = world.apply(lst["id"], world.creator(full_name="Late Applicant"))
    assert client.get(_list_url(role, lst["id"])).status_code == 200
    d = client.get(f"{_list_url(role, lst['id'])}/{aid}")
    assert d.status_code == 200 and SECRET in d.text


@ROLES
def test_taken_down_listing_is_not_found_for_its_poster_too(client, world, role):
    me, lst = _poster(client, world, role)
    world.db.tables["creator_job_listings"][-1]["is_taken_down"] = True
    aid = world.apply(lst["id"], world.creator())
    assert client.get(_list_url(role, lst["id"])).status_code == 404
    d = client.get(f"{_list_url(role, lst['id'])}/{aid}")
    assert d.status_code == 404 and SECRET not in d.text


@ROLES
def test_read_failure_is_an_error_not_a_fake_empty_state(client, world, role):
    me, lst = _poster(client, world, role)
    world.apply(lst["id"], world.creator())
    world.db.fail_tables.add("creator_job_applications")
    r = client.get(_list_url(role, lst["id"]))
    assert r.status_code == 503
    assert "couldn't load applicants" in r.text
    assert "No applicants yet" not in r.text


@ROLES
def test_long_names_titles_and_messages_render_in_full(client, world, role):
    me = world.brand() if role == "brand" else world.creator(full_name="P")
    _sign_in(client, me, role)
    title = "Extremely long opportunity title " + ("campaign " * 12) + "U" * 40
    lst = world.listing(me, title=title[:140], poster_role=role)
    name = "Maximilian-Alexander " + "Wolfeschlegelsteinhausen" * 3
    unbroken = "u" * 2000
    spaced = ("great fit " * 300)[:2000]
    a1 = world.apply(lst["id"], world.creator(full_name=name), message=unbroken, when="2026-03-05T00:00:00Z")
    a2 = world.apply(lst["id"], world.creator(full_name="Normal"), message=spaced, when="2026-03-04T00:00:00Z")
    lst_page = client.get(_list_url(role, lst["id"]))
    assert lst_page.status_code == 200
    assert title[:140] in lst_page.text and name in lst_page.text
    d1 = client.get(f"{_list_url(role, lst['id'])}/{a1}")
    d2 = client.get(f"{_list_url(role, lst['id'])}/{a2}")
    assert d1.status_code == d2.status_code == 200
    assert unbroken in d1.text  # all 2000 characters, nothing truncated
    assert spaced in d2.text


# ==================================================== routes: authorization


@ROLES
def test_non_poster_cannot_open_the_applicants_list(client, world, role):
    owner = world.brand()
    lst = world.listing(owner)
    world.apply(lst["id"], world.creator(full_name="Private Person"))
    intruder = world.brand() if role == "brand" else world.creator()
    _sign_in(client, intruder, role)
    r = client.get(_list_url(role, lst["id"]))
    assert r.status_code == 403
    assert "Private Person" not in r.text and SECRET not in r.text


@ROLES
def test_non_poster_cannot_open_an_application(client, world, role):
    owner = world.brand()
    lst = world.listing(owner)
    aid = world.apply(lst["id"], world.creator())
    intruder = world.brand() if role == "brand" else world.creator()
    _sign_in(client, intruder, role)
    r = client.get(f"{_list_url(role, lst['id'])}/{aid}")
    assert r.status_code == 403
    assert SECRET not in r.text


def test_another_applicant_cannot_read_someone_elses_application(client, world):
    owner = world.brand()
    lst = world.listing(owner)
    victim, snoop = world.creator(), world.creator()
    victim_app = world.apply(lst["id"], victim, message=SECRET)
    snoop_app = world.apply(lst["id"], snoop, message="snoop's own note")
    _sign_in(client, snoop, "creator")
    for url in (
        f"/creator/jobs/{lst['id']}/applicants",
        f"/creator/jobs/{lst['id']}/applicants/{victim_app}",
        f"/creator/jobs/{lst['id']}/applicants/{snoop_app}",  # not even their own, via this route
    ):
        r = client.get(url)
        assert r.status_code == 403, url
        assert SECRET not in r.text


@ROLES
def test_url_ids_cannot_be_swapped_to_expose_another_listings_application(client, world, role):
    me, lst_a = _poster(client, world, role)
    lst_b = world.listing(me, title="Second campaign", poster_role=role)
    app_b = world.apply(lst_b["id"], world.creator(), message=SECRET)
    # application belongs to B but is requested under A (both mine)
    r = client.get(f"{_list_url(role, lst_a['id'])}/{app_b}")
    assert r.status_code == 404
    assert SECRET not in r.text
    # and the right pairing works
    assert client.get(f"{_list_url(role, lst_b['id'])}/{app_b}").status_code == 200


@ROLES
def test_application_of_a_foreign_listing_cannot_be_read_via_my_listing(client, world, role):
    me, mine = _poster(client, world, role)
    foreign = world.listing(world.brand(), title="Not mine")
    foreign_app = world.apply(foreign["id"], world.creator(), message=SECRET)
    # via my listing id -> pinned to my listing -> 404
    r = client.get(f"{_list_url(role, mine['id'])}/{foreign_app}")
    assert r.status_code == 404 and SECRET not in r.text
    # via their listing id -> I am not the poster -> 403
    r = client.get(f"{_list_url(role, foreign['id'])}/{foreign_app}")
    assert r.status_code == 403 and SECRET not in r.text


@ROLES
def test_listing_ownership_mismatch_fails_closed(client, world, role):
    owner = world.brand()
    lst = world.listing(owner)
    aid = world.apply(lst["id"], world.creator())
    me = world.brand() if role == "brand" else world.creator()
    _sign_in(client, me, role)
    # claim ownership through every client-controlled channel
    r = client.get(
        f"{_list_url(role, lst['id'])}/{aid}",
        params={"poster_user_id": me, "owner": me, "user_id": me, "role": "brand"},
        headers={"X-User-Id": me, "X-Role": "brand"},
    )
    assert r.status_code == 403 and SECRET not in r.text


@ROLES
@pytest.mark.parametrize("bad", ["not-a-uuid", "1' or '1'='1", "%00", "..%2f..%2fetc", "x" * 80])
def test_malformed_ids_fail_safely(client, world, role, bad):
    me, lst = _poster(client, world, role)
    aid = world.apply(lst["id"], world.creator())
    assert client.get(_list_url(role, bad)).status_code in (404, 422)
    r = client.get(f"{_list_url(role, lst['id'])}/{bad}")
    assert r.status_code == 404 and SECRET not in r.text
    r = client.get(f"{_list_url(role, bad)}/{aid}")
    assert r.status_code in (404, 422) and SECRET not in r.text


@ROLES
def test_missing_application_and_missing_listing_fail_safely(client, world, role):
    me, lst = _poster(client, world, role)
    world.apply(lst["id"], world.creator())
    assert client.get(f"{_list_url(role, lst['id'])}/{uuid4()}").status_code == 404
    assert client.get(_list_url(role, str(uuid4()))).status_code == 404
    assert client.get(f"{_list_url(role, str(uuid4()))}/{uuid4()}").status_code == 404


def test_role_guards_and_anonymous_access(client, world):
    owner = world.brand()
    lst = world.listing(owner)
    aid = world.apply(lst["id"], world.creator())
    # wrong role tree
    _sign_in(client, world.creator(), "creator")
    r = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}")
    assert r.status_code == 403 and SECRET not in r.text
    _sign_in(client, owner, "brand")
    r = client.get(f"/creator/jobs/{lst['id']}/applicants/{aid}")
    assert r.status_code == 403 and SECRET not in r.text
    # anonymous
    client.cookies.clear()
    for url in (
        f"/creator/jobs/{lst['id']}/applicants",
        f"/creator/jobs/{lst['id']}/applicants/{aid}",
        f"/brand/discover/opportunity/{lst['id']}/applicants",
        f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}",
    ):
        r = client.get(url)
        assert r.status_code in (302, 303, 401, 403), url
        assert SECRET not in r.text


def test_brand_onboarding_gate_is_respected(client, world):
    me = world.brand(onboarding_completed_at=None)
    _sign_in(client, me, "brand")
    lst = world.listing(me)
    aid = world.apply(lst["id"], world.creator())
    for url in (
        f"/brand/discover/opportunity/{lst['id']}/applicants",
        f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}",
    ):
        r = client.get(url)
        assert r.status_code == 302 and r.headers["location"] == "/onboarding/brand"
        assert SECRET not in r.text


def test_authorization_does_not_depend_on_poster_role_column(client, world):
    """``poster_role`` has historically been unreliable; ``poster_user_id`` rules."""
    me = world.brand()
    _sign_in(client, me, "brand")
    lst = world.listing(me, poster_role="creator")  # mislabelled, still mine
    aid = world.apply(lst["id"], world.creator())
    assert client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}").status_code == 200


# ============================================ entry points: My opportunities


def test_brand_my_posted_opportunity_routes_into_applicants_and_it_opens(client, world):
    me, lst = _brand_poster(client, world)
    world.apply(lst["id"], world.creator(full_name="Alex Kim"))
    page = client.get("/brand/discover?kind=opportunity&view=mine")
    links = _card_links(page.text)
    assert links == [f"/brand/discover/opportunity/{lst['id']}/applicants"]
    assert "1 applicant" in page.text
    opened = client.get(links[0])
    assert opened.status_code == 200 and "Alex Kim" in opened.text


def test_creator_my_posted_opportunity_routes_into_applicants_and_it_opens(client, world):
    me, lst = _creator_poster(client, world)
    world.apply(lst["id"], world.creator(full_name="Alex Kim"))
    page = client.get("/creator/discover?kind=opportunity&view=mine")
    links = _card_links(page.text)
    assert links == [f"/creator/jobs/{lst['id']}/applicants"]
    assert "1 applicant" in page.text
    opened = client.get(links[0])
    assert opened.status_code == 200 and "Alex Kim" in opened.text


def test_my_applied_opportunity_still_routes_to_the_step_5a_detail_with_applied_state(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    _sign_in(client, me, "creator")
    world.apply(lst["id"], me, message=SECRET)
    page = client.get("/creator/discover?kind=opportunity&view=mine")
    links = _card_links(page.text)
    assert links == [f"/creator/jobs/{lst['id']}"]  # NOT the applicant review
    assert "/applicants" not in page.text
    assert "✓ Applied" in page.text
    detail = client.get(links[0])
    assert detail.status_code == 200
    assert "✓ Applied" in detail.text
    # an applicant is refused the review surface for that same listing
    assert client.get(f"/creator/jobs/{lst['id']}/applicants").status_code == 403


def test_creator_mine_shows_posted_first_then_applied_and_each_keeps_its_own_state(client, world):
    me = world.creator()
    _sign_in(client, me, "creator")
    posted = world.listing(me, poster_role="creator", title="I posted this")
    theirs = world.listing(world.brand(), title="I applied here")
    world.apply(posted["id"], world.creator())
    world.apply(posted["id"], world.creator())
    world.apply(theirs["id"], me)
    page = client.get("/creator/discover?kind=opportunity&view=mine")
    assert _card_links(page.text) == [
        f"/creator/jobs/{posted['id']}/applicants",
        f"/creator/jobs/{theirs['id']}",
    ]
    assert page.text.index("I posted this") < page.text.index("I applied here")
    assert "2 applicants" in page.text and "✓ Applied" in page.text


def test_my_opportunities_cards_never_carry_application_content(client, world):
    me, lst = _brand_poster(client, world)
    a = world.creator(full_name="Hidden Applicant Name")
    world.apply(lst["id"], a, message=SECRET)
    page = client.get("/brand/discover?kind=opportunity&view=mine")
    assert SECRET not in page.text and "Hidden Applicant Name" not in page.text
    creator_me = world.creator()
    other = world.listing(world.brand(), title="Applied elsewhere")
    world.apply(other["id"], creator_me, message=SECRET)
    _sign_in(client, creator_me, "creator")
    page = client.get("/creator/discover?kind=opportunity&view=mine")
    assert SECRET not in page.text


def test_explore_links_from_ac8bbfe_are_unchanged(client, world, monkeypatch):
    poster = world.brand()
    lst = world.listing(poster)
    card = {
        "card_kind": "opportunity",
        "card_id": lst["id"],
        "owner_user_id": poster,
        "title": lst["title"],
        "subtitle": "Olipop",
        "tags": [],
        "description": "x",
        "detail_path": f"/creator/jobs/{lst['id']}",
        "relevance_reasons": [],
    }
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [dict(card)])
    _sign_in(client, world.creator(), "creator")
    assert _card_links(client.get("/creator/discover?kind=opportunity").text) == [
        f"/creator/jobs/{lst['id']}"
    ]
    _sign_in(client, world.brand(), "brand")
    r = client.get("/brand/discover?kind=opportunity")
    assert _card_links(r.text) == [f"/brand/discover/opportunity/{lst['id']}"]
    assert "/applicants" not in r.text


def test_profile_card_links_from_5ef0477_are_unchanged(client, world, monkeypatch):
    uid = str(uuid4())
    card = {
        "card_kind": "creator",
        "card_id": uid,
        "owner_user_id": uid,
        "title": "Sam Rivera",
        "subtitle": "@sam",
        "tags": [],
        "description": "x",
        "detail_path": f"/creator/network/{uid}",
        "relevance_reasons": [],
    }
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [dict(card)])
    _sign_in(client, world.creator(), "creator")
    assert _card_links(client.get("/creator/discover?kind=creator").text) == [
        f"/creator/network/{uid}"
    ]
    _sign_in(client, world.brand(), "brand")
    assert _card_links(client.get("/brand/discover?kind=creator").text) == [
        f"/brand/discover/creator/{uid}"
    ]
    from app.core import templating

    assert templating._safe_url("/creator/network/x") == "#"  # safe_url untouched
    assert templating._safe_path("/creator/network/x") == "/creator/network/x"


def test_step_5b_submission_still_works_and_lands_in_the_poster_review(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator(full_name="Fresh Applicant")
    _sign_in(client, applicant, "creator")
    r = client.post(f"/creator/jobs/{lst['id']}/apply", data={"message": "Pick me — I'm great."})
    assert r.status_code == 303 and r.headers["location"] == f"/creator/jobs/{lst['id']}/applied"
    # duplicate still refused
    assert client.post(f"/creator/jobs/{lst['id']}/apply", data={"message": "again"}).status_code in (
        303,
        400,
    )
    assert len(world.db.tables["creator_job_applications"]) == 1
    # the poster now sees exactly that application
    _sign_in(client, poster, "brand")
    page = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants")
    assert "Fresh Applicant" in page.text
    aid = _row_links(page.text)[0].rsplit("/", 1)[1]
    detail = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}")
    assert "Pick me — I&#39;m great." in detail.text or "Pick me — I'm great." in detail.text


# ========================================== review only: nothing beyond that


@ROLES
def test_pages_are_review_only_no_forms_buttons_or_offer_controls(client, world, role):
    me, lst = _poster(client, world, role)
    aid = world.apply(lst["id"], world.creator(), message="Plain message.")
    for url in (_list_url(role, lst["id"]), f"{_list_url(role, lst['id'])}/{aid}"):
        html = client.get(url).text
        article = html[html.index('<article class="wrap opportunity-detail opportunity-applicants">') :]
        article = article[: article.index("</article>")].lower()
        assert "<form" not in article and "<button" not in article and "<input" not in article
        if url.endswith(aid):
            # Step 6A adds exactly one action here: a "Make offer" LINK
            assert article.count("make offer") == 1
            assert re.findall(r"\boffer\b", article) == ["offer", "offer"]  # label + path
            article = article.replace("make offer", "").replace("/offer", "")
        for word in (
            "offer", "accept", "reject", "decline", "shortlist", "hire", "deal",
            "checkout", "payment", "payout", "stripe", "message them", "dm",
        ):
            assert not re.search(rf"\b{word}\b", article), (word, url)


def test_no_write_routes_exist_under_the_review_paths():
    for route in app.routes:
        path = getattr(route, "path", "")
        if "/applicants" in path and not path.endswith("/applicants/{application_id}/offer"):
            assert set(route.methods or ()) <= {"GET", "HEAD"}, (path, route.methods)


def test_review_paths_reject_writes(client, world):
    me, lst = _brand_poster(client, world)
    aid = world.apply(lst["id"], world.creator())
    for url in (
        f"/brand/discover/opportunity/{lst['id']}/applicants",
        f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}",
    ):
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)(url).status_code in (404, 405)
    assert len(world.db.tables["creator_job_applications"]) == 1


def test_no_migration_or_schema_change_is_part_of_step_5d():
    names = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert "0048_creator_job_applications.sql" in names
    assert [n for n in names if n[:4] > "0048"] == [
        "0049_creator_job_offers.sql",  # Step 6A
        "0050_creator_job_offer_responses.sql",  # Step 6B
    ]
    sql = Path("migrations/0048_creator_job_applications.sql").read_text(encoding="utf-8")
    assert "status = 'submitted'" in sql  # still the single Step 5B status


def test_step_5d_touches_no_offer_deal_payment_or_integration_code():
    import ast

    banned = ("stripe", "instagram", "gmail", "google", "oauth", "webhook", "dms", "payout", "deal", "offer")
    tree = ast.parse(Path("app/services/job_applications.py").read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [f"{node.module}.{a.name}" for a in node.names]
    assert sorted(imported) == sorted(
        [
            "__future__.annotations",
            "logging",
            "typing.Any",
            "postgrest.exceptions.APIError",
            "app.core.supabase_client",
            "app.core.uuid_guard.safe_uuid",
            "app.services.jobs",
            "app.services.profiles",
        ]
    ), imported
    for name in imported:
        assert not any(b in name.lower() for b in banned), name
    for rel in (
        "app/templates/creator/opportunity_applicants.html",
        "app/templates/creator/opportunity_application.html",
    ):
        src = Path(rel).read_text(encoding="utf-8").lower()
        for word in ("stripe", "oauth", "webhook", "payout", "checkout", "<form", "<button"):
            assert word not in src, (rel, word)
