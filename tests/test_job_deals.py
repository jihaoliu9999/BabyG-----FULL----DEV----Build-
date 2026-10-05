"""Step 6C: an accepted offer becomes exactly one Deal; both parties see it.

Flow under test:

    Offer accepted (Step 6B) -> the DB creates exactly one deal (0051 trigger)
      -> DMs: messages | offers | deals (creator) / messages | deals (brand)
      -> Deals list -> Deal detail (locked terms + "Deal active. Payment is
         the next step.")

The in-memory PostgREST fake below emulates migrations 0049-0051, including
the AFTER UPDATE trigger that inserts the deal in the same statement as the
accept (ON CONFLICT (offer_id) DO NOTHING). The REAL ``job_offers``,
``job_deals``, ``job_applications``, ``jobs`` and ``profiles`` services run
against it. Application code never inserts a deal -- tests assert that too.
"""

from __future__ import annotations

import importlib
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import csrf as _csrf_module
from app.core import supabase_client
from app.core.security import SESSION_COOKIE, write_session

_REAL_CSRF_CALL = _csrf_module.CSRFMiddleware.__call__

from app.main import app  # noqa: E402
from app.services import (  # noqa: E402
    action_proposals,
    discovery,
    dm_briefs,
    dms,
    instagram_dms,
    job_deals,
    job_offers,
    network,
    views,
)
from app.services import discover as discover_service  # noqa: E402

_TABLES = ("creator_job_listings", "creator_job_applications", "creator_profiles",
           "brand_profiles", "creator_job_offers", "creator_job_deals")
DELIVERABLES = "2 TikToks\n1 Instagram Reel"


def _err(code: str, msg: str) -> PostgrestAPIError:
    return PostgrestAPIError({"message": msg, "code": code, "hint": None, "details": None})


class _Query:
    def __init__(self, db: _FakeDB, name: str) -> None:
        self.db, self.name = db, name
        self.op = "select"
        self.cols: list[str] | None = None
        self.filters: list[tuple[str, str, Any]] = []
        self.orders: list[tuple[str, bool]] = []
        self.lim: int | None = None
        self.body: dict[str, Any] | None = None

    def select(self, cols: str = "*") -> _Query:
        self.op, self.cols = "select", (None if cols.strip() == "*" else [c.strip() for c in cols.split(",")])
        return self

    def insert(self, body: dict[str, Any]) -> _Query:
        self.op, self.body = "insert", dict(body)
        return self

    def update(self, body: dict[str, Any]) -> _Query:
        self.op, self.body = "update", dict(body)
        return self

    def eq(self, col: str, val: Any) -> _Query:
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col: str, val: Any) -> _Query:
        self.filters.append(("is", col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> _Query:
        self.filters.append(("in", col, list(vals)))
        return self

    def order(self, col: str, desc: bool = False) -> _Query:
        self.orders.append((col, desc))
        return self

    def limit(self, n: int) -> _Query:
        self.lim = n
        return self

    def _match(self, r: dict[str, Any]) -> bool:
        for kind, col, val in self.filters:
            if kind == "eq" and str(r.get(col)) != str(val):
                return False
            if kind == "is" and val == "null" and r.get(col) is not None:
                return False
            if kind == "in" and str(r.get(col)) not in {str(v) for v in val}:
                return False
        return True

    def execute(self) -> SimpleNamespace:
        self.db.queries.append({"table": self.name, "op": self.op, "filters": list(self.filters),
                                "body": self.body})
        if self.name in self.db.fail_tables:
            raise _err("500", "boom")
        rows = self.db.tables[self.name]
        if self.op == "insert":
            assert self.body is not None
            if self.name == "creator_job_deals":
                self.db.insert_deal(self.body)  # emulated UNIQUE (offer_id) raises
                return SimpleNamespace(data=[dict(rows[-1])])
            row = {"id": str(uuid4()), "created_at": self.db.now(), **self.body}
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [r for r in rows if self._match(r)]
        if self.op == "update":
            assert self.body is not None
            out = []
            for r in matched:
                before = dict(r)
                cand = {**r, **self.body}
                if self.name == "creator_job_offers":
                    if cand.get("status") not in ("sent", "accepted", "declined"):
                        raise _err("23514", "creator_job_offers_status_check")
                    if (cand["status"] != "sent") != (cand.get("responded_at") is not None):
                        raise _err("23514", "creator_job_offers_response_consistency")
                r.update(self.body)
                out.append(dict(r))
                # migration 0051: AFTER UPDATE OF status ... WHEN accepted -> create deal
                if (self.name == "creator_job_offers" and "status" in self.body
                        and r["status"] == "accepted" and before.get("status") != "accepted"):
                    self.db.create_deal_from_offer(r)
            return SimpleNamespace(data=out)
        for col, desc in reversed(self.orders):
            matched.sort(key=lambda r, c=col: str(r.get(c) or ""), reverse=desc)
        if self.lim is not None:
            matched = matched[: self.lim]
        if self.cols is not None:
            matched = [{c: r.get(c) for c in self.cols} for r in matched]
        return SimpleNamespace(data=[dict(r) for r in matched])


class _FakeDB:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {t: [] for t in _TABLES}
        self.queries: list[dict[str, Any]] = []
        self.fail_tables: set[str] = set()
        self._tick = 0

    def now(self) -> str:
        self._tick += 1
        return (datetime(2026, 10, 1, tzinfo=UTC) + timedelta(minutes=self._tick)).isoformat()

    def table(self, name: str) -> _Query:
        if name not in self.tables:
            raise RuntimeError("supabase env missing (test fake)")
        return _Query(self, name)

    def insert_deal(self, body: dict[str, Any]) -> None:
        if any(d["offer_id"] == body["offer_id"] for d in self.deals):
            raise _err("23505", "creator_job_deals_one_per_offer")
        self.deals.append({"id": str(uuid4()), "status": "active", "currency": "USD",
                           "created_at": self.now(), **body})

    def create_deal_from_offer(self, o: dict[str, Any]) -> None:
        """The 0051 trigger body: copy the stored offer; ON CONFLICT DO NOTHING."""
        if any(d["offer_id"] == o["id"] for d in self.deals):
            return
        self.insert_deal({k: o[k] for k in ("application_id", "listing_id", "poster_user_id",
                                            "applicant_user_id", "amount_cents", "currency",
                                            "deliverables", "due_date")} | {"offer_id": o["id"]})

    @property
    def offers(self) -> list[dict[str, Any]]:
        return self.tables["creator_job_offers"]

    @property
    def deals(self) -> list[dict[str, Any]]:
        return self.tables["creator_job_deals"]

    def app_writes_to_deals(self) -> list[dict[str, Any]]:
        return [q for q in self.queries if q["table"] == "creator_job_deals" and q["op"] != "select"]


class _World:
    def __init__(self, db: _FakeDB) -> None:
        self.db = db
        self.native_unread = 0

    def creator(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        row = {"user_id": uid, "full_name": kw.pop("full_name", "Alex Kim"), "instagram_handle": "alexk",
               "profile_photo_url": kw.pop("profile_photo_url", None),
               "onboarding_completed_at": "2026-01-01T00:00:00Z", "niches": [], "content_formats": [],
               "hard_limits": [], "bio": None}
        row.update(kw)
        self.db.tables["creator_profiles"].append(row)
        return uid

    def brand(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        self.db.tables["brand_profiles"].append({
            "user_id": uid, "company_name": kw.pop("company_name", "Olipop"), "logo_url": None,
            "onboarding_completed_at": kw.pop("onboarding_completed_at", "2026-01-01T00:00:00Z"),
            "niche_preferences": []})
        return uid

    def listing(self, poster: str, **kw: Any) -> dict[str, Any]:
        row = {"id": str(uuid4()), "poster_user_id": poster, "poster_role": kw.pop("poster_role", "brand"),
               "title": kw.pop("title", "Fall Campaign — Miami"), "description": "Reels.",
               "listing_type": "brand_deal", "compensation_text": "$2k", "budget_min": None,
               "budget_max": None, "target_niches": [], "deadline": None,
               "is_active": kw.pop("is_active", True), "is_taken_down": kw.pop("is_taken_down", False),
               "discovery_eligible": True, "expires_at": None, "location_city": None,
               "location_region": None, "created_at": "2026-01-01T00:00:00Z"}
        row.update(kw)
        self.db.tables["creator_job_listings"].append(row)
        return row

    def offer(self, listing: dict[str, Any], applicant: str, **kw: Any) -> str:
        aid = str(uuid4())
        self.db.tables["creator_job_applications"].append(
            {"id": aid, "listing_id": listing["id"], "applicant_user_id": applicant, "message": "Pick me.",
             "status": "submitted", "created_at": "2026-03-04T10:30:00Z"})
        oid = str(uuid4())
        self.db.offers.append({
            "id": oid, "application_id": aid, "listing_id": listing["id"],
            "poster_user_id": listing["poster_user_id"], "applicant_user_id": applicant,
            "amount_cents": kw.pop("amount_cents", 100000), "currency": "USD",
            "deliverables": kw.pop("deliverables", DELIVERABLES), "due_date": kw.pop("due_date", "2026-10-30"),
            "note": None, "status": "sent", "viewed_at": None, "responded_at": None,
            "created_at": self.db.now()})
        return oid


@pytest.fixture()
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> _World:
    w = _World(db)
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: db)
    monkeypatch.setattr(dms, "list_threads_for_user", lambda uid: [])
    monkeypatch.setattr(dms, "unread_counts_by_thread", lambda uid, ids: {})
    monkeypatch.setattr(dms, "unread_count_for_user", lambda uid: w.native_unread)
    monkeypatch.setattr(dms, "last_messages_by_thread", lambda ids: {})
    monkeypatch.setattr(dm_briefs, "latest_briefs_for_threads", lambda ids, recipient_id: {})
    monkeypatch.setattr(instagram_dms, "unread_count_for_creator", lambda uid: 0)
    monkeypatch.setattr(action_proposals, "count_pending_for_user", lambda user_id: 0)
    monkeypatch.setattr(network, "get_connection_between", lambda a, b: None)
    monkeypatch.setattr(views, "record_view", lambda **kw: True)
    monkeypatch.setattr(discovery, "record_action", lambda **kw: True)
    for name, fn in (("last_undoable_pass", lambda uid: None), ("record_action", lambda **kw: True),
                     ("list_cards", lambda **kw: []), ("get_opportunity_cards", lambda ids: {}),
                     ("get_card", lambda **kw: None)):
        monkeypatch.setattr(discover_service, name, fn)
    return w


@pytest.fixture(autouse=True)
def no_side_effect_systems(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payments, Stripe, payouts, deal memory, notifications and LLM calls must never run."""
    guarded = {
        "app.services.stripe_client": None, "app.integrations.stripe_client": None,
        "app.services.creator_payouts": None, "app.services.babyg_deals": None,
        "app.services.deal_manager": None, "app.services.notifications": {"create"},
        "app.services.dms": {"send_message", "get_or_create_thread", "send"},
        "app.integrations.anthropic_client": None,
    }
    for mod_name, only in guarded.items():
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for attr in dir(mod):
            if attr.startswith("_") or not callable(getattr(mod, attr, None)):
                continue
            if isinstance(getattr(mod, attr), type) or (only is not None and attr not in only):
                continue

            def _boom(*a: Any, _m: str = mod_name, _a: str = attr, **k: Any) -> None:
                raise AssertionError(f"Step 6C touched {_m}.{_a}")

            monkeypatch.setattr(mod, attr, _boom, raising=False)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, uid: str, role: str = "creator") -> str:
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])
    return uid


def _accepted(client, world, *, poster_role: str = "brand", **kw: Any) -> SimpleNamespace:
    """A brand (or creator) poster, an applicant, an offer -- accepted through
    the real Step 6B route, which is what makes the deal exist."""
    poster = world.brand(company_name=kw.pop("company_name", "Olipop")) if poster_role == "brand" \
        else world.creator(full_name=kw.pop("poster_name", "Poster Creator"))
    lst = world.listing(poster, poster_role=poster_role, title=kw.pop("title", "Fall Campaign — Miami"))
    me = world.creator(full_name=kw.pop("applicant_name", "Sofia Ramirez"))
    oid = world.offer(lst, me, **kw)
    _sign_in(client, me)
    r = client.post(f"/creator/dm/offers/{oid}/accept")
    assert r.status_code == 303, r.text[:300]
    [deal] = [d for d in world.db.deals if d["offer_id"] == oid]
    return SimpleNamespace(poster=poster, lst=lst, me=me, oid=oid, deal=deal, poster_role=poster_role)


def _deal_links(html: str, base: str) -> list[str]:
    return re.findall(rf'<a href="{re.escape(base)}([0-9a-f-]+)" class="dm-inbox-link">', html)


def _tabs(html: str, base: str) -> list[tuple[str, str]]:
    return re.findall(rf'<a href="({re.escape(base)}[^"]*)"[^>]*>([a-z]+)', html.split('aria-label="dms view"', 1)[1].split("</nav>", 1)[0])


# ================================================================ DEAL CREATION


def test_accepting_creates_exactly_one_deal_with_the_exact_offer_terms(client, world):
    s = _accepted(client, world, amount_cents=125050, deliverables=DELIVERABLES, due_date="2026-10-30")
    offer = next(o for o in world.db.offers if o["id"] == s.oid)
    assert len(world.db.deals) == 1
    d = s.deal
    for k in ("application_id", "listing_id", "poster_user_id", "applicant_user_id",
              "amount_cents", "currency", "deliverables", "due_date"):
        assert d[k] == offer[k], k
    assert d["offer_id"] == s.oid and d["status"] == "active" and d["currency"] == "USD"
    assert d["amount_cents"] == 125050 and isinstance(d["amount_cents"], int)
    assert d["poster_user_id"] == s.poster and d["applicant_user_id"] == s.me


def test_application_code_never_inserts_a_deal(client, world):
    _accepted(client, world)
    assert world.db.app_writes_to_deals() == []  # only the (emulated) trigger did
    src = Path("app/services/job_deals.py").read_text(encoding="utf-8")
    assert ".insert(" not in src and ".update(" not in src and ".delete(" not in src
    for route in ("app/routes/creator.py", "app/routes/brand.py"):
        assert 'table("creator_job_deals")' not in Path(route).read_text(encoding="utf-8")


def test_declined_and_sent_offers_create_no_deal(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    declined, sent = world.offer(lst, me), world.offer(lst, me)
    _sign_in(client, me)
    assert client.post(f"/creator/dm/offers/{declined}/decline").status_code == 303
    client.get(f"/creator/dm/offers/{sent}")  # viewing is not accepting
    assert world.db.deals == []


def test_repeat_accept_and_revisits_never_duplicate(client, world):
    s = _accepted(client, world)
    for _ in range(3):
        client.post(f"/creator/dm/offers/{s.oid}/accept")
        client.post(f"/creator/dm/offers/{s.oid}/decline")
        client.get(f"/creator/dm/offers/{s.oid}")
        client.get(f"/creator/dm/deals/{s.deal['id']}")
        client.get("/creator/dm?view=deals")
    assert len(world.db.deals) == 1
    assert next(o for o in world.db.offers if o["id"] == s.oid)["status"] == "accepted"


def test_racing_accepts_produce_one_deal(world, monkeypatch):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    oid = world.offer(lst, me)
    stale = job_offers.get_received(oid, me)
    assert job_offers.respond(oid, me, "accepted")[0] == job_offers.RESPONDED
    real, calls = job_offers.get_received, {"n": 0}

    def _stale_first(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return dict(stale) if calls["n"] == 1 else real(*a, **k)

    monkeypatch.setattr(job_offers, "get_received", _stale_first)
    assert job_offers.respond(oid, me, "accepted")[0] == job_offers.ALREADY_DECIDED
    assert len(world.db.deals) == 1


def test_db_unique_offer_constraint_rejects_a_second_deal(world):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    oid = world.offer(lst, me)
    job_offers.respond(oid, me, "accepted")
    with pytest.raises(PostgrestAPIError):
        world.db.table("creator_job_deals").insert({**{k: world.db.deals[0][k] for k in (
            "offer_id", "application_id", "listing_id", "poster_user_id", "applicant_user_id",
            "amount_cents", "deliverables", "due_date")}}).execute()
    assert len(world.db.deals) == 1


# ================================================================ AUTHORIZATION


def test_applicant_and_brand_poster_can_open_the_deal(client, world):
    s = _accepted(client, world)
    assert client.get(f"/creator/dm/deals/{s.deal['id']}").status_code == 200
    _sign_in(client, s.poster, "brand")
    assert client.get(f"/brand/dm/deals/{s.deal['id']}").status_code == 200


def test_creator_poster_opens_the_same_deal_in_creator_dms(client, world):
    s = _accepted(client, world, poster_role="creator")
    _sign_in(client, s.poster)
    r = client.get(f"/creator/dm/deals/{s.deal['id']}")
    assert r.status_code == 200 and "Sofia Ramirez" in r.text


def test_unrelated_users_get_404_on_both_trees(client, world):
    s = _accepted(client, world)
    _sign_in(client, world.creator())
    assert client.get(f"/creator/dm/deals/{s.deal['id']}").status_code == 404
    _sign_in(client, world.brand(), "brand")
    assert client.get(f"/brand/dm/deals/{s.deal['id']}").status_code == 404


def test_id_tampering_between_two_deals_is_blocked(client, world):
    a = _accepted(client, world)
    b = _accepted(client, world, applicant_name="Other Person")
    _sign_in(client, a.me)
    assert client.get(f"/creator/dm/deals/{b.deal['id']}").status_code == 404
    assert client.get(f"/creator/dm/deals/{a.deal['id']}").status_code == 200


@pytest.mark.parametrize("bad", ["not-a-uuid", "1' or '1'='1", "x" * 80, str(uuid4())])
def test_malformed_and_missing_ids_are_404(client, world, bad):
    _accepted(client, world)
    assert client.get(f"/creator/dm/deals/{bad}").status_code == 404


def test_unauthenticated_and_wrong_role_are_refused(client, world):
    s = _accepted(client, world)
    client.cookies.clear()
    for url in (f"/creator/dm/deals/{s.deal['id']}", f"/brand/dm/deals/{s.deal['id']}",
                "/creator/dm?view=deals", "/brand/dm?view=deals"):
        assert client.get(url).status_code in (302, 303, 401, 403), url
    _sign_in(client, s.poster, "brand")
    assert client.get(f"/creator/dm/deals/{s.deal['id']}").status_code == 403
    _sign_in(client, s.me)
    assert client.get(f"/brand/dm/deals/{s.deal['id']}").status_code == 403


def test_service_reads_are_party_scoped(world, db):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    oid = world.offer(lst, me)
    job_offers.respond(oid, me, "accepted")
    did = db.deals[0]["id"]
    assert job_deals.get_for_user(did, me) is not None
    assert job_deals.get_for_user(did, poster) is not None
    assert job_deals.get_for_user(did, world.creator()) is None
    assert job_deals.deal_id_for_offer(oid, me) == did
    assert job_deals.deal_id_for_offer(oid, world.brand()) is None
    assert job_deals.list_for_user(world.creator()) == []
    assert job_deals.get_for_user("nope", me) is None and job_deals.deal_id_for_offer("nope", me) is None


# ================================================================== DEALS LIST


def test_both_parties_see_the_deal_with_the_other_partys_identity(client, world):
    s = _accepted(client, world, company_name="Olipop Beverages", applicant_name="Sofia Ramirez",
                  amount_cents=100000, due_date="2026-10-30")
    html = client.get("/creator/dm?view=deals").text
    assert _deal_links(html, "/creator/dm/deals/") == [s.deal["id"]]
    row = html.split("data-dm-deal>", 1)[1].split("</li>", 1)[0]
    assert "Olipop Beverages" in row and "Sofia Ramirez" not in row      # applicant sees the brand
    assert "Fall Campaign — Miami" in row
    assert '<span class="dm-deal-terms"><strong>$1,000</strong> · active</span>' in row
    assert '<span class="dm-inbox-time">oct 30, 2026</span>' in row
    _sign_in(client, s.poster, "brand")
    html = client.get("/brand/dm?view=deals").text
    assert _deal_links(html, "/brand/dm/deals/") == [s.deal["id"]]
    row = html.split("data-dm-deal>", 1)[1].split("</li>", 1)[0]
    assert "Sofia Ramirez" in row and "Olipop" not in row                # poster sees the applicant


def test_unrelated_user_sees_no_deals(client, world):
    _accepted(client, world)
    _sign_in(client, world.creator())
    html = client.get("/creator/dm?view=deals").text
    assert _deal_links(html, "/creator/dm/deals/") == [] and "no deals yet." in html


def test_creator_who_is_poster_and_applicant_sees_both_kinds(client, world):
    me = world.creator(full_name="Dual Role")
    # I am the applicant on a brand's offer...
    brand = world.brand(company_name="BrandCo")
    oid1 = world.offer(world.listing(brand), me)
    # ...and the poster of my own creator listing
    other = world.creator(full_name="My Applicant")
    oid2 = world.offer(world.listing(me, poster_role="creator", title="My collab"), other)
    _sign_in(client, me)
    client.post(f"/creator/dm/offers/{oid1}/accept")
    _sign_in(client, other)
    client.post(f"/creator/dm/offers/{oid2}/accept")
    _sign_in(client, me)
    html = client.get("/creator/dm?view=deals").text
    assert len(_deal_links(html, "/creator/dm/deals/")) == 2
    assert "BrandCo" in html and "My Applicant" in html


def test_ordering_is_newest_first_then_id(world, db):
    brand = world.brand()
    me = world.creator()
    ids = []
    for _ in range(3):
        oid = world.offer(world.listing(brand), me)
        job_offers.respond(oid, me, "accepted")
        ids.append(next(d["id"] for d in db.deals if d["offer_id"] == oid))
    assert [d["id"] for d in job_deals.list_for_user(me)] == list(reversed(ids))
    for d in db.deals:
        d["created_at"] = "2026-10-01T00:00:00+00:00"
    assert [d["id"] for d in job_deals.list_for_user(me)] == sorted(ids)


@pytest.mark.parametrize(("cents", "shown"), [(1, "$0.01"), (25050, "$250.50"), (1_000_000_000, "$10,000,000")])
def test_amount_formatting(client, world, cents, shown):
    _accepted(client, world, amount_cents=cents)
    assert f"<strong>{shown}</strong> · active" in client.get("/creator/dm?view=deals").text


def test_empty_failed_and_hidden_states(client, world):
    s = _accepted(client, world)
    world.db.fail_tables.add("creator_job_deals")
    html = client.get("/creator/dm?view=deals").text
    assert "couldn't load deals." in html and "no deals yet." not in html
    world.db.fail_tables.clear()
    world.db.tables["creator_job_listings"][-1]["is_taken_down"] = True
    html = client.get("/creator/dm?view=deals").text
    assert _deal_links(html, "/creator/dm/deals/") == [] and "no deals yet." in html
    assert client.get(f"/creator/dm/deals/{s.deal['id']}").status_code == 404


# ================================================================= DEAL DETAIL


def test_deal_detail_shows_locked_terms_identity_and_babyg_state(client, world):
    s = _accepted(client, world, amount_cents=100000, deliverables=DELIVERABLES, due_date="2026-10-30",
                  company_name="Olipop")
    r = client.get(f"/creator/dm/deals/{s.deal['id']}")
    html = r.text
    art = html.split("<article", 1)[1].split("</article>", 1)[0]
    assert '<a href="/creator/dm?view=deals" class="back-link">← deals</a>' in art
    assert "<strong>Olipop</strong>" in art and "<span>brand</span>" in art
    assert '<span class="badge badge-muted">active</span>' in art
    assert '<h1 class="detail-title">$1,000</h1>' in art
    assert f'<p class="opportunity-detail-description">{DELIVERABLES}</p>' in art
    assert "<h2>due date</h2>\n    <p>oct 30, 2026</p>" in art
    assert "<h2>babyg</h2>" in art and "<p>Deal active.</p><p>Payment is the next step.</p>" in art
    # hierarchy order
    order = [art.index(x) for x in ("back-link", "Olipop", ">active<", "$1,000", "deliverables",
                                     "due date", "original opportunity", "<h2>babyg</h2>")]
    assert order == sorted(order)
    # nothing beyond Step 6C
    assert "<form" not in art and "<button" not in art
    for banned in ("pay now", "confirm payment", "checkout", "payout", "fee", "mark complete",
                   "track", "milestone", "follow up", "deal id", s.deal["id"]):
        assert banned not in art.lower(), banned
    assert "Babyg" not in art and "BabyG" not in art


def test_original_opportunity_link_uses_the_existing_authorized_route(client, world):
    s = _accepted(client, world)
    html = client.get(f"/creator/dm/deals/{s.deal['id']}").text
    assert f'<a href="/creator/jobs/{s.lst["id"]}" class="opportunity-deal-link">Fall Campaign — Miami</a>' in html
    assert client.get(f"/creator/jobs/{s.lst['id']}").status_code == 200
    # closed listing: the applicant's route would 404, so no link (title only)
    world.db.tables["creator_job_listings"][-1]["is_active"] = False
    html = client.get(f"/creator/dm/deals/{s.deal['id']}").text
    assert f'href="/creator/jobs/{s.lst["id"]}"' not in html and "Fall Campaign — Miami" in html
    # the brand poster always reaches their own listing
    _sign_in(client, s.poster, "brand")
    html = client.get(f"/brand/dm/deals/{s.deal['id']}").text
    assert f'<a href="/brand/discover/opportunity/{s.lst["id"]}" class="opportunity-deal-link">' in html
    assert client.get(f"/brand/discover/opportunity/{s.lst['id']}").status_code == 200


def test_deal_detail_is_a_normal_scrolling_page_not_the_chat_shell(client, world):
    s = _accepted(client, world)
    html = client.get(f"/creator/dm/deals/{s.deal['id']}").text
    body = re.search(r"<body[^>]*>", html).group(0)
    assert "is-dm" not in body
    assert 'data-tab="inbox"\n     class="active"' in html  # DMs tab stays active


# ============================================================ OFFER INTEGRATION


def test_accepted_offer_review_links_to_its_deal(client, world):
    s = _accepted(client, world)
    html = client.get(f"/creator/dm/offers/{s.oid}").text
    assert "✓ Offer accepted" in html
    assert f'<a href="/creator/dm/deals/{s.deal["id"]}" class="btn btn-lime">View deal</a>' in html


@pytest.mark.parametrize("poster_role", ["brand", "creator"])
def test_poster_application_page_shows_view_deal(client, world, poster_role):
    s = _accepted(client, world, poster_role=poster_role)
    app_row = next(o for o in world.db.offers if o["id"] == s.oid)
    if poster_role == "brand":
        _sign_in(client, s.poster, "brand")
        url = f"/brand/discover/opportunity/{s.lst['id']}/applicants/{app_row['application_id']}"
        deal_url = f"/brand/dm/deals/{s.deal['id']}"
    else:
        _sign_in(client, s.poster)
        url = f"/creator/jobs/{s.lst['id']}/applicants/{app_row['application_id']}"
        deal_url = f"/creator/dm/deals/{s.deal['id']}"
    html = client.get(url).text
    assert f'<a href="{deal_url}" class="btn btn-lime">View deal</a>' in html
    assert "✓ Offer sent" not in html and "Make offer" not in html
    assert client.get(deal_url).status_code == 200


def test_declined_and_pending_offers_keep_their_6b_and_6a_states(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    declined, pending = world.offer(lst, me), world.offer(lst, me)
    _sign_in(client, me)
    client.post(f"/creator/dm/offers/{declined}/decline")
    html = client.get(f"/creator/dm/offers/{declined}").text
    assert "Offer declined" in html and "View deal" not in html
    _sign_in(client, poster, "brand")
    for oid in (declined, pending):
        app_id = next(o for o in world.db.offers if o["id"] == oid)["application_id"]
        page = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{app_id}").text
        assert "✓ Offer sent" in page and "View deal" not in page


def test_offers_inbox_and_unread_badges_are_unchanged(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    me = world.creator()
    a, b = world.offer(lst, me), world.offer(lst, me)
    world.native_unread = 2
    _sign_in(client, me)
    html = client.get("/creator/dm?view=offers").text
    assert html.count("<span class=\"dm-inbox-count-dot\">new</span>") == 2
    assert re.search(r'data-tabbar-badge="dms">4<', html)   # 2 messages + 2 offers
    client.post(f"/creator/dm/offers/{a}/accept")             # -> deal
    html = client.get("/creator/dm?view=deals").text
    assert re.search(r'data-tabbar-badge="dms">3<', html)   # deals add NO attention
    assert ">deals</a>" in html and ">deals<span" not in html  # no Deals badge
    html = client.get("/creator/dm?view=offers").text
    assert '<span class="dm-inbox-time">accepted</span>' in html and b in html


# =================================================================== DMs TABS


def test_creator_tabs_are_messages_offers_deals(client, world):
    _sign_in(client, world.creator())
    for view, active in (("", "messages"), ("?view=offers", "offers"), ("?view=deals", "deals"),
                         ("?view=bogus", "messages")):
        html = client.get(f"/creator/dm{view}").text
        assert [t[1] for t in _tabs(html, "/creator/dm")] == ["messages", "offers", "deals"]
        assert f'aria-current="page">{active}' in html


def test_brand_tabs_are_messages_deals_and_the_placeholder_is_unchanged(client, world):
    _sign_in(client, world.brand(), "brand")
    html = client.get("/brand/dm").text
    assert _tabs(html, "/brand/dm") == [("/brand/dm", "messages"), ("/brand/dm?view=deals", "deals")]
    assert "brand messaging is coming soon" in html and 'aria-current="page">messages' in html
    html = client.get("/brand/dm?view=deals").text
    assert "no deals yet." in html and "brand messaging is coming soon" not in html
    assert 'aria-current="page">deals' in html


def test_messages_view_keeps_babyg_manager_pinned(client, world):
    _sign_in(client, world.creator())
    html = client.get("/creator/dm").text
    assert "data-dm-manager" in html and 'placeholder="Search conversations"' in html
    assert "data-dm-deals" not in html


def test_csrf_still_guards_accept(world, monkeypatch):
    monkeypatch.setattr(_csrf_module.CSRFMiddleware, "__call__", _REAL_CSRF_CALL)
    c = TestClient(app, follow_redirects=False)
    me = world.creator()
    oid = world.offer(world.listing(world.brand()), me)
    _sign_in(c, me)
    assert c.post(f"/creator/dm/offers/{oid}/accept", headers={"Origin": "http://testserver"}).status_code == 403
    assert world.db.deals == []


# ============================================================ SCOPE / MIGRATION


def test_no_payment_completion_or_deal_management_surface():
    # (the pre-existing babyg Manager /creator/deals pages are a separate,
    # untouched feature; Step 6C deals live under DMs only)
    paths = sorted({getattr(r, "path", "") for r in app.routes if "/dm/deals" in getattr(r, "path", "")})
    assert paths == ["/brand/dm/deals/{deal_id}", "/creator/dm/deals/{deal_id}"]
    for r in app.routes:
        if "/dm/deals" in getattr(r, "path", ""):
            assert set(r.methods or ()) <= {"GET", "HEAD"}
    assert job_deals.BABYG_STATE == ("Deal active.", "Payment is the next step.")
    tpl = re.sub(r"\{#.*?#\}", "", Path("app/templates/creator/deal_detail.html").read_text(encoding="utf-8"),
                 flags=re.S).lower()
    for banned in ("<form", "<button", "stripe", "pay now", "payout", "checkout", "fee", "complete"):
        assert banned not in tpl, banned


MIGRATION = Path("migrations/0051_creator_job_deals.sql")


def test_migration_0051_is_additive_private_and_atomic():
    names = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert names[-1] == MIGRATION.name and names[-2] == "0050_creator_job_offer_responses.sql"
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).lower()
    assert code.strip().startswith("begin;") and code.strip().endswith("commit;")
    assert "create table if not exists public.creator_job_deals" in code
    assert "constraint creator_job_deals_one_per_offer unique (offer_id)" in code
    assert "check (status = 'active')" in code and "check (currency = 'usd')" in code
    assert "amount_cents bigint not null check (amount_cents > 0 and amount_cents <= 1000000000)" in code
    assert "references public.creator_job_offers(id)" in code
    assert "enable row level security" in code
    assert "revoke all on public.creator_job_deals from anon, authenticated" in code
    assert "grant select, insert on public.creator_job_deals to service_role" in code
    assert "after update of status on public.creator_job_offers" in code
    assert "when (new.status = 'accepted' and old.status is distinct from 'accepted')" in code
    assert code.count("on conflict (offer_id) do nothing") == 2  # trigger + backfill
    assert "where o.status = 'accepted'" in code
    for banned in ("drop table", "drop column", "truncate", "delete from", "create policy",
                   " to authenticated", " to anon", "alter table public.creator_job_offers add",
                   "paid", "funded", "completed", "refunded", "disputed", "stripe", "payout"):
        assert banned not in code, banned
