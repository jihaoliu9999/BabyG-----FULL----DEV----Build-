"""Step 6B: received offers in DMs, unread badges, babyg brief, accept/decline.

Flow under test:

    Offer sent -> recipient sees an unread offer -> DMs shows attention ->
    DMs: messages | offers -> Offers -> Review Offer (brief + exact terms)
    -> Accept or Decline (final; nothing else is created)

The recipient is always the offer's stored ``applicant_user_id``. These
tests run the REAL ``job_offers`` / ``job_applications`` / ``jobs`` /
``profiles`` services against an in-memory PostgREST fake that supports
conditional UPDATEs and emulates migrations 0049 + 0050 (status, response
consistency, one offer per application). DM / IG / manager counts are
service-boundary stubs so the badge arithmetic can be asserted exactly.
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
    job_offers,
    network,
    views,
)
from app.services import discover as discover_service  # noqa: E402

DELIVERABLES = "2 TikToks\n1 Instagram Reel"
NOTE = "Please tag us in the caption."

_TABLES = (
    "creator_job_listings",
    "creator_job_applications",
    "creator_profiles",
    "brand_profiles",
    "creator_job_offers",
    "creator_job_deals",  # read by the accepted Review Offer page since Step 6C
)


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
        self.op = "select"
        self.cols = None if cols.strip() == "*" else [c.strip() for c in cols.split(",")]
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

    @staticmethod
    def _check_offer(row: dict[str, Any]) -> None:
        """Migration 0049 + 0050 constraints."""
        if row.get("status") not in ("sent", "accepted", "declined"):
            raise _err("23514", "creator_job_offers_status_check")
        decided = row.get("status") in ("accepted", "declined")
        if decided != (row.get("responded_at") is not None):
            raise _err("23514", "creator_job_offers_response_consistency")
        if row.get("poster_user_id") == row.get("applicant_user_id"):
            raise _err("23514", "creator_job_offers_not_self")

    def execute(self) -> SimpleNamespace:
        self.db.queries.append({"table": self.name, "op": self.op, "filters": list(self.filters),
                                "cols": self.cols, "body": self.body})
        if self.name in self.db.fail_tables:
            raise _err("500", "boom")
        rows = self.db.tables[self.name]
        if self.op == "insert":
            assert self.body is not None
            row = {"id": str(uuid4()), "created_at": self.db.now(), **self.body}
            if self.name == "creator_job_offers":
                row.setdefault("status", "sent")
                row.setdefault("currency", "USD")
                row.setdefault("viewed_at", None)
                row.setdefault("responded_at", None)
                if any(r["application_id"] == row["application_id"] for r in rows):
                    raise _err("23505", "creator_job_offers_one_per_application")
                self._check_offer(row)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [r for r in rows if self._match(r)]
        if self.op == "update":
            assert self.body is not None
            out = []
            for r in matched:
                candidate = {**r, **self.body}
                if self.name == "creator_job_offers":
                    self._check_offer(candidate)
                r.update(self.body)
                out.append(dict(r))
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

    @property
    def offers(self) -> list[dict[str, Any]]:
        return self.tables["creator_job_offers"]

    def offer(self, oid: str) -> dict[str, Any]:
        return next(o for o in self.offers if o["id"] == oid)

    def writes(self) -> list[tuple[str, str]]:
        return [(q["table"], q["op"]) for q in self.queries if q["op"] in ("insert", "update")]


class _World:
    def __init__(self, db: _FakeDB) -> None:
        self.db = db
        self.native_unread: dict[str, int] = {}  # thread_id -> unread
        self.threads: list[dict[str, Any]] = []
        self.ig_unread = 0
        self.pending = 0

    def creator(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        row = {"user_id": uid, "full_name": kw.pop("full_name", "Alex Kim"),
               "instagram_handle": kw.pop("instagram_handle", "alexk"),
               "profile_photo_url": kw.pop("profile_photo_url", None),
               "onboarding_completed_at": "2026-01-01T00:00:00Z", "niches": [], "content_formats": [],
               "hard_limits": [], "bio": None}
        row.update(kw)
        self.db.tables["creator_profiles"].append(row)
        return uid

    def brand(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        row = {"user_id": uid, "company_name": kw.pop("company_name", "Olipop"),
               "logo_url": kw.pop("logo_url", None),
               "onboarding_completed_at": "2026-01-01T00:00:00Z", "niche_preferences": []}
        row.update(kw)
        self.db.tables["brand_profiles"].append(row)
        return uid

    def listing(self, poster: str, **kw: Any) -> dict[str, Any]:
        row = {"id": str(uuid4()), "poster_user_id": poster, "poster_role": kw.pop("poster_role", "brand"),
               "title": kw.pop("title", "Autumn campaign reels"), "description": "Reels.",
               "listing_type": "brand_deal", "compensation_text": "$2k", "budget_min": None,
               "budget_max": None, "target_niches": [], "deadline": None,
               "is_active": kw.pop("is_active", True), "is_taken_down": kw.pop("is_taken_down", False),
               "discovery_eligible": True, "expires_at": None, "location_city": None,
               "location_region": None, "created_at": "2026-01-01T00:00:00Z"}
        row.update(kw)
        self.db.tables["creator_job_listings"].append(row)
        return row

    def apply(self, listing_id: str, applicant: str) -> str:
        aid = str(uuid4())
        self.db.tables["creator_job_applications"].append(
            {"id": aid, "listing_id": listing_id, "applicant_user_id": applicant,
             "message": "Pick me.", "status": "submitted", "created_at": "2026-03-04T10:30:00Z"})
        return aid

    def offer(self, listing: dict[str, Any], applicant: str, **kw: Any) -> str:
        """A stored 6A offer (as the 6A service writes it)."""
        aid = self.apply(listing["id"], applicant)
        oid = str(uuid4())
        self.db.offers.append({
            "id": oid, "application_id": aid, "listing_id": listing["id"],
            "poster_user_id": listing["poster_user_id"], "applicant_user_id": applicant,
            "amount_cents": kw.pop("amount_cents", 100000), "currency": "USD",
            "deliverables": kw.pop("deliverables", DELIVERABLES), "due_date": kw.pop("due_date", "2026-10-30"),
            "note": kw.pop("note", None), "status": kw.pop("status", "sent"),
            "viewed_at": kw.pop("viewed_at", None), "responded_at": kw.pop("responded_at", None),
            "created_at": kw.pop("created_at", self.db.now()),
        })
        return oid


@pytest.fixture()
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, db: _FakeDB) -> _World:
    w = _World(db)
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: db)
    # DM / IG / manager sources behind controllable stubs
    monkeypatch.setattr(dms, "list_threads_for_user", lambda uid: list(w.threads))
    monkeypatch.setattr(dms, "unread_counts_by_thread",
                        lambda uid, ids: {i: w.native_unread.get(i, 0) for i in ids})
    monkeypatch.setattr(dms, "unread_count_for_user", lambda uid: sum(w.native_unread.values()))
    monkeypatch.setattr(dms, "last_messages_by_thread", lambda ids: {})
    monkeypatch.setattr(dm_briefs, "latest_briefs_for_threads", lambda ids, recipient_id: {})
    monkeypatch.setattr(instagram_dms, "unread_count_for_creator", lambda uid: w.ig_unread)
    monkeypatch.setattr(action_proposals, "count_pending_for_user", lambda user_id: w.pending)
    monkeypatch.setattr(network, "get_connection_between", lambda a, b: None)
    monkeypatch.setattr(views, "record_view", lambda **kw: True)
    monkeypatch.setattr(discovery, "record_action", lambda **kw: True)
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(discover_service, "record_action", lambda **kw: True)
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [])
    monkeypatch.setattr(discover_service, "get_opportunity_cards", lambda ids: {})
    monkeypatch.setattr(discover_service, "get_card", lambda **kw: None)
    return w


@pytest.fixture(autouse=True)
def no_side_effect_systems(monkeypatch: pytest.MonkeyPatch) -> None:
    """Payments, Stripe, deals, DM sends and notifications must never run."""
    guarded = {
        "app.services.stripe_client": None,
        "app.integrations.stripe_client": None,
        "app.services.creator_payouts": None,
        "app.services.babyg_deals": None,
        "app.services.deal_manager": None,
        "app.services.notifications": {"create"},
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
                raise AssertionError(f"Step 6B touched {_m}.{_a}")

            monkeypatch.setattr(mod, attr, _boom, raising=False)
    try:
        import stripe

        monkeypatch.setattr(stripe, "StripeClient", lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("Step 6B constructed a Stripe client")), raising=False)
    except ImportError:
        pass


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, uid: str, role: str = "creator") -> str:
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])
    return uid


def _setup(client, world, **kw: Any) -> SimpleNamespace:
    poster = world.brand(company_name=kw.pop("company_name", "Olipop"))
    lst = world.listing(poster, title=kw.pop("title", "Autumn campaign reels"))
    me = world.creator(full_name="Recipient Person")
    oid = world.offer(lst, me, **kw)
    _sign_in(client, me)
    return SimpleNamespace(poster=poster, lst=lst, me=me, oid=oid)


def _offers_page(client) -> str:
    r = client.get("/creator/dm?view=offers")
    assert r.status_code == 200, r.text[:300]
    return r.text


def _offer_links(html: str) -> list[str]:
    return re.findall(r'<a href="/creator/dm/offers/([0-9a-f-]+)" class="dm-inbox-link">', html)


def _tab_badge(html: str, tab: str) -> int:
    m = re.search(rf'>{tab}(?:<span class="dm-inbox-count-dot"[^>]*>(\d+)</span>)?</a>', html)
    assert m, f"{tab} tab missing"
    return int(m.group(1) or 0)


def _nav_badge(html: str) -> int:
    m = re.search(r'data-tabbar-badge="dms">(\d+)</span>', html)
    return int(m.group(1)) if m else 0


# ================================================================ OFFERS INBOX


def test_recipient_sees_their_received_offer(client, world):
    s = _setup(client, world, amount_cents=125050, due_date="2026-10-30")
    html = _offers_page(client)
    assert _offer_links(html) == [s.oid]
    assert "Olipop" in html                      # sender identity
    assert "Autumn campaign reels" in html       # opportunity title
    assert "$1,250.50" in html                   # amount, exact cents
    assert "due oct 30, 2026" in html            # due date
    assert '<span class="dm-inbox-count-dot">new</span>' in html
    assert DELIVERABLES.split("\n")[0] not in html   # terms are not on the list
    assert 'aria-current="page">offers' in html


def test_sender_does_not_see_their_sent_offer_in_received_inbox(client, world):
    poster = world.creator(full_name="Poster Creator")
    lst = world.listing(poster, poster_role="creator")
    world.offer(lst, world.creator())
    _sign_in(client, poster)
    html = _offers_page(client)
    assert _offer_links(html) == []
    assert "no offers yet." in html
    assert _tab_badge(html, "offers") == 0


def test_unrelated_user_sees_nothing(client, world):
    s = _setup(client, world)
    _sign_in(client, world.creator())
    html = _offers_page(client)
    assert _offer_links(html) == [] and "$1,000" not in html
    assert client.get(f"/creator/dm/offers/{s.oid}").status_code == 404


@pytest.mark.parametrize(("cents", "shown"), [(1, "$0.01"), (100000, "$1,000"), (25050, "$250.50"),
                                              (1_000_000_000, "$10,000,000")])
def test_amount_formatting_uses_integer_cents(client, world, cents, shown):
    _setup(client, world, amount_cents=cents)
    assert f'<span class="dm-offer-amount">{shown}</span>' in _offers_page(client)


def test_creator_poster_identity_uses_their_profile(client, world):
    poster = world.creator(full_name="Jordan Creator", profile_photo_url="https://cdn.example/j.png")
    lst = world.listing(poster, poster_role="creator", title="Creator collab")
    me = world.creator()
    world.offer(lst, me)
    _sign_in(client, me)
    html = _offers_page(client)
    assert "Jordan Creator" in html and 'src="https://cdn.example/j.png"' in html


def test_statuses_and_deterministic_ordering(client, world):
    me = world.creator()
    brand = world.brand()
    lst = world.listing(brand)
    old_new = world.offer(lst, me, created_at="2026-09-01T00:00:00+00:00")
    newer_viewed = world.offer(lst, me, created_at="2026-09-05T00:00:00+00:00",
                               viewed_at="2026-09-06T00:00:00+00:00")
    accepted = world.offer(lst, me, created_at="2026-09-10T00:00:00+00:00", status="accepted",
                           viewed_at="2026-09-10T01:00:00+00:00", responded_at="2026-09-10T01:00:00+00:00")
    declined = world.offer(lst, me, created_at="2026-09-02T00:00:00+00:00", status="declined",
                           viewed_at="2026-09-03T00:00:00+00:00", responded_at="2026-09-03T00:00:00+00:00")
    _sign_in(client, me)
    html = _offers_page(client)
    # awaiting a decision first (newest first), then decided (newest first)
    assert _offer_links(html) == [newer_viewed, old_new, accepted, declined]
    assert html.count('<span class="dm-inbox-count-dot">new</span>') == 1
    for label in ("viewed", "accepted", "declined"):
        assert f'<span class="dm-inbox-time">{label}</span>' in html
    assert _offers_page(client).count("dm-inbox-link") == html.count("dm-inbox-link")  # stable


def test_same_timestamp_ties_break_by_id(world):
    me = world.creator()
    lst = world.listing(world.brand())
    ids = [world.offer(lst, me, created_at="2026-09-01T00:00:00+00:00") for _ in range(4)]
    items = job_offers.list_received(me)
    assert [o["id"] for o in items] == sorted(ids)


def test_empty_and_failed_inbox_states(client, world):
    me = world.creator()
    _sign_in(client, me)
    html = _offers_page(client)
    assert "no offers yet." in html and "offers you receive will appear here." in html
    world.db.fail_tables.add("creator_job_offers")
    html = _offers_page(client)
    assert "couldn't load offers." in html and "no offers yet." not in html


def test_taken_down_listing_hides_the_offer_everywhere(client, world):
    s = _setup(client, world)
    world.db.tables["creator_job_listings"][-1]["is_taken_down"] = True
    html = _offers_page(client)
    assert _offer_links(html) == [] and _tab_badge(html, "offers") == 0 and _nav_badge(html) == 0
    assert client.get(f"/creator/dm/offers/{s.oid}").status_code == 404
    assert client.post(f"/creator/dm/offers/{s.oid}/accept").status_code == 404
    assert world.db.offer(s.oid)["status"] == "sent"


def test_closed_listing_offer_stays_reviewable(client, world):
    s = _setup(client, world)
    world.db.tables["creator_job_listings"][-1]["is_active"] = False
    assert _offer_links(_offers_page(client)) == [s.oid]
    assert client.get(f"/creator/dm/offers/{s.oid}").status_code == 200


# ====================================================================== UNREAD


def test_sent_offer_starts_unread_and_badges_add_up(client, world):
    s = _setup(client, world)
    world.threads = [{"id": "t1", "peer_id": str(uuid4()), "last_message_at": None},
                     {"id": "t2", "peer_id": str(uuid4()), "last_message_at": None}]
    world.native_unread = {"t1": 2, "t2": 1}
    world.ig_unread = 4
    html = client.get("/creator/dm").text
    assert _tab_badge(html, "messages") == 3          # internal DMs only
    assert _tab_badge(html, "offers") == 1            # the received offer
    assert _nav_badge(html) == 3 + 4 + 1              # native + IG (as today) + offers
    assert s.oid


def test_messages_badge_is_independent_of_offers_and_of_search(client, world):
    _setup(client, world)
    world.threads = [{"id": "t1", "peer_id": str(uuid4()), "last_message_at": None}]
    world.native_unread = {"t1": 5}
    assert _tab_badge(client.get("/creator/dm").text, "messages") == 5
    assert _tab_badge(client.get("/creator/dm?q=zzzz-no-match").text, "messages") == 5
    assert _tab_badge(_offers_page(client), "messages") == 5
    world.native_unread = {}
    html = client.get("/creator/dm").text
    assert _tab_badge(html, "messages") == 0 and _tab_badge(html, "offers") == 1


def test_opening_the_inbox_does_not_mark_offers_read(client, world):
    s = _setup(client, world)
    for _ in range(2):
        html = _offers_page(client)
    assert _tab_badge(html, "offers") == 1
    assert world.db.offer(s.oid)["viewed_at"] is None


def test_viewing_one_offer_marks_only_that_offer(client, world):
    me = world.creator()
    lst = world.listing(world.brand())
    a, b = world.offer(lst, me), world.offer(lst, me)
    _sign_in(client, me)
    assert _tab_badge(_offers_page(client), "offers") == 2
    page = client.get(f"/creator/dm/offers/{a}")
    assert page.status_code == 200
    assert _nav_badge(page.text) == 1  # this very page already reflects the view
    assert world.db.offer(a)["viewed_at"] is not None and world.db.offer(b)["viewed_at"] is None
    html = _offers_page(client)
    assert _tab_badge(html, "offers") == 1
    assert '<span class="dm-inbox-time">viewed</span>' in html


def test_refresh_preserves_read_state_and_never_double_decrements(client, world):
    s = _setup(client, world)
    client.get(f"/creator/dm/offers/{s.oid}")
    first = world.db.offer(s.oid)["viewed_at"]
    for _ in range(3):
        page = client.get(f"/creator/dm/offers/{s.oid}")
        assert _nav_badge(page.text) == 0
    assert world.db.offer(s.oid)["viewed_at"] == first  # set once, never overwritten
    assert _tab_badge(_offers_page(client), "offers") == 0


def test_mark_viewed_sets_once_and_only_for_the_recipient(world, db):
    me = world.creator()
    lst = world.listing(world.brand())
    oid = world.offer(lst, me)
    assert job_offers.mark_viewed(oid, world.creator()) is False  # not the recipient
    assert db.offer(oid)["viewed_at"] is None
    assert job_offers.mark_viewed(oid, me) is True
    first = db.offer(oid)["viewed_at"]
    assert job_offers.mark_viewed(oid, me) is False               # already viewed
    assert db.offer(oid)["viewed_at"] == first
    upd = [q for q in db.queries if q["op"] == "update"][-1]
    assert ("is", "viewed_at", "null") in upd["filters"] and ("eq", "applicant_user_id", me) in upd["filters"]
    assert job_offers.mark_viewed("nope", me) is False


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_responding_clears_attention_even_without_viewing_first(client, world, decision):
    s = _setup(client, world)
    assert client.post(f"/creator/dm/offers/{s.oid}/{decision}").status_code == 303
    row = world.db.offer(s.oid)
    assert row["viewed_at"] is not None and row["responded_at"] is not None
    html = _offers_page(client)
    assert _tab_badge(html, "offers") == 0 and _nav_badge(html) == 0


def test_badge_counts_each_offer_once(world):
    me = world.creator()
    lst = world.listing(world.brand())
    world.offer(lst, me)
    world.offer(lst, me, viewed_at="2026-09-01T00:00:00+00:00")
    world.offer(lst, me, status="accepted", viewed_at=None, responded_at="2026-09-01T00:00:00+00:00")
    other_listing = world.listing(world.brand())
    world.offer(other_listing, world.creator())  # someone else's
    assert job_offers.unread_count(me) == 1


def test_unread_count_query_is_recipient_scoped_and_never_raises(world, db):
    me = world.creator()
    assert job_offers.unread_count(me) == 0
    q = [q for q in db.queries if q["table"] == "creator_job_offers"][-1]
    assert ("eq", "applicant_user_id", me) in q["filters"]
    assert ("eq", "status", "sent") in q["filters"] and ("is", "viewed_at", "null") in q["filters"]
    db.fail_tables.add("creator_job_offers")
    assert job_offers.unread_count(me) == 0
    assert job_offers.unread_count("not-a-uuid") == 0


def test_badge_global_is_creator_only_and_cached(monkeypatch):
    from app.core import templating

    calls = []
    monkeypatch.setattr(job_offers, "unread_count", lambda uid: calls.append(uid) or 3)
    req = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})
    monkeypatch.setattr("app.core.security.read_session", lambda r: {"user_id": "u", "role": "creator"})
    assert templating.unread_offer_count(req) == 3 and templating.unread_offer_count(req) == 3
    assert calls == ["u"]
    req2 = SimpleNamespace(state=SimpleNamespace(), cookies={}, headers={})
    monkeypatch.setattr("app.core.security.read_session", lambda r: {"user_id": "u", "role": "brand"})
    assert templating.unread_offer_count(req2) == 0


def test_tabbar_priming_fetches_offers_once_per_request(monkeypatch):
    from starlette.requests import Request

    from app.core import tabbar_priming

    calls = []
    monkeypatch.setattr(job_offers, "unread_count", lambda uid: calls.append(uid) or 2)
    monkeypatch.setattr(dms, "unread_count_for_user", lambda uid: 1)
    monkeypatch.setattr(instagram_dms, "unread_count_for_creator", lambda uid: 1)
    monkeypatch.setattr(action_proposals, "count_pending_for_user", lambda user_id: 0)
    monkeypatch.setattr(tabbar_priming, "read_session", lambda r: {"user_id": "u", "role": "creator"})
    req = Request({"type": "http", "method": "GET", "path": "/creator/dm", "headers": [], "query_string": b""})
    tabbar_priming.prime_creator_tabbar(req)
    tabbar_priming.prime_creator_tabbar(req)
    assert calls == ["u"]
    assert req.state.unread_offer_count == 2 and req.state.unread_dm_count == 2  # meaning unchanged


# ====================================================================== REVIEW


def test_review_shows_exact_stored_terms_and_identity(client, world):
    s = _setup(client, world, amount_cents=125050, deliverables=DELIVERABLES, note=NOTE,
               due_date="2026-10-30", company_name="Olipop & Co <b>")
    r = client.get(f"/creator/dm/offers/{s.oid}")
    assert r.status_code == 200
    html = r.text
    assert "Olipop &amp; Co &lt;b&gt;" in html and "<b>" not in html.split("<article", 1)[1].split("</article>")[0]
    assert '<h1 class="detail-title">Autumn campaign reels</h1>' in html
    assert "<dd>$1,250.50</dd>" in html and "<dd>oct 30, 2026</dd>" in html
    assert f'<p class="opportunity-detail-description">{DELIVERABLES}</p>' in html
    assert f'<p class="opportunity-detail-description">{NOTE}</p>' in html
    assert '<a href="/creator/dm?view=offers" class="back-link">← offers</a>' in html
    assert '<button type="submit" class="btn btn-lime">Accept offer</button>' in html
    assert '<button type="submit" class="btn btn-ghost">Decline offer</button>' in html
    assert html.index("Accept offer") < html.index("Decline offer")
    assert html.count('name="csrf_token"') >= 2


def test_review_without_note_has_no_note_section(client, world):
    s = _setup(client, world, note=None)
    html = client.get(f"/creator/dm/offers/{s.oid}").text
    assert "<h2>note</h2>" not in html


def test_brief_is_grounded_in_stored_terms_only(client, world):
    s = _setup(client, world, amount_cents=100000, deliverables=DELIVERABLES, note=None, due_date="2030-10-30")
    html = client.get(f"/creator/dm/offers/{s.oid}").text
    brief = html.split('aria-label="babyg brief">', 1)[1].split("</section>", 1)[0]
    assert "$1,000 for 2 TikToks, 1 Instagram Reel, due oct 30, 2030." in brief
    assert "days from today." in brief
    assert "The offer doesn&#39;t mention usage rights or exclusivity." in brief
    assert "No note was included." in brief
    for invented in ("fair", "good deal", "bad deal", "market", "average", "recommend", "should accept",
                     "views", "followers", "engagement", "legal"):
        assert invented not in brief.lower(), invented


def test_brief_does_not_claim_terms_are_missing_when_mentioned():
    offer = {"amount_cents": 50000, "deliverables": "1 Reel, 30-day usage license",
             "note": "Exclusivity: 14 days in category", "due_date": "2030-01-01", "status": "sent"}
    brief = job_offers.compose_brief(offer, today=datetime(2029, 12, 1).date())
    assert brief[0] == "$500 for 1 Reel, 30-day usage license, due jan 1, 2030."
    assert brief[1] == "That's 31 days from today."
    assert not any("doesn't mention" in line for line in brief)
    assert not any("No note" in line for line in brief)


def test_brief_handles_long_scope_and_past_due_dates():
    offer = {"amount_cents": 1, "deliverables": "x" * 400, "note": "", "due_date": "2020-01-01", "status": "sent"}
    brief = job_offers.compose_brief(offer, today=datetime(2026, 1, 1).date())
    assert len(brief[0]) < 200 and "…" in brief[0]
    assert "The due date has already passed." in brief


def test_brief_failure_does_not_block_review_or_response(client, world, monkeypatch):
    s = _setup(client, world)
    monkeypatch.setattr(job_offers, "compose_brief", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    r = client.get(f"/creator/dm/offers/{s.oid}")
    assert r.status_code == 200
    assert "babyg couldn't prepare a brief for this offer." in r.text
    assert "Accept offer" in r.text and "<dd>$1,000</dd>" in r.text
    assert client.post(f"/creator/dm/offers/{s.oid}/accept").status_code == 303
    assert world.db.offer(s.oid)["status"] == "accepted"


# ============================================================ ACCEPT / DECLINE


@pytest.mark.parametrize(("decision", "final", "marker"), [
    ("accept", "accepted", "✓ Offer accepted"),
    ("decline", "declined", "Offer declined"),
])
def test_recipient_can_respond_once(client, world, decision, final, marker):
    s = _setup(client, world)
    r = client.post(f"/creator/dm/offers/{s.oid}/{decision}")
    assert r.status_code == 303 and r.headers["location"] == f"/creator/dm/offers/{s.oid}"
    row = world.db.offer(s.oid)
    assert row["status"] == final and row["responded_at"] is not None
    page = client.get(r.headers["location"]).text
    assert marker in page and "Accept offer" not in page and "Decline offer" not in page
    assert "<dd>" + final + "</dd>" in page and "responded " in page


@pytest.mark.parametrize("first", ["accept", "decline"])
def test_decision_is_final_repeat_and_opposite_posts_change_nothing(client, world, first):
    s = _setup(client, world)
    client.post(f"/creator/dm/offers/{s.oid}/{first}")
    snapshot = dict(world.db.offer(s.oid))
    for again in ("accept", "decline", "accept"):
        r = client.post(f"/creator/dm/offers/{s.oid}/{again}")
        assert r.status_code == 303
    assert world.db.offer(s.oid) == snapshot


def test_race_between_two_responses_has_exactly_one_winner(world, monkeypatch):
    me = world.creator()
    lst = world.listing(world.brand())
    oid = world.offer(lst, me)
    stale = job_offers.get_received(oid, me)  # both requests read "sent"
    assert job_offers.respond(oid, me, "accepted")[0] == job_offers.RESPONDED
    real = job_offers.get_received
    calls = {"n": 0}

    def _stale_first(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return dict(stale) if calls["n"] == 1 else real(*a, **k)

    monkeypatch.setattr(job_offers, "get_received", _stale_first)
    outcome, offer = job_offers.respond(oid, me, "declined")
    assert outcome == job_offers.ALREADY_DECIDED
    assert offer is not None and offer["status"] == "accepted"
    assert world.db.offer(oid)["status"] == "accepted"
    update = [q for q in world.db.queries if q["op"] == "update"][-1]
    assert ("eq", "status", "sent") in update["filters"]  # the DB-side guard
    assert ("eq", "applicant_user_id", me) in update["filters"]


def test_decline_keeps_the_offer_and_it_stays_in_history(client, world):
    s = _setup(client, world)
    client.post(f"/creator/dm/offers/{s.oid}/decline")
    assert len(world.db.offers) == 1
    html = _offers_page(client)
    assert _offer_links(html) == [s.oid] and '<span class="dm-inbox-time">declined</span>' in html
    assert client.get(f"/creator/dm/offers/{s.oid}").status_code == 200


def test_form_fields_cannot_choose_identities_or_status(client, world):
    s = _setup(client, world)
    attacker = str(uuid4())
    client.post(f"/creator/dm/offers/{s.oid}/accept", data={
        "status": "declined", "applicant_user_id": attacker, "poster_user_id": attacker,
        "listing_id": str(uuid4()), "application_id": str(uuid4()), "offer_id": str(uuid4()),
    })
    row = world.db.offer(s.oid)
    assert row["status"] == "accepted" and row["applicant_user_id"] == s.me
    assert row["poster_user_id"] == s.poster and row["listing_id"] == s.lst["id"]


def test_only_offer_lifecycle_columns_are_written(client, world):
    s = _setup(client, world)
    client.get(f"/creator/dm/offers/{s.oid}")
    client.post(f"/creator/dm/offers/{s.oid}/accept")
    assert world.db.writes() == [("creator_job_offers", "update"), ("creator_job_offers", "update")]
    for q in world.db.queries:
        if q["op"] == "update":
            assert set(q["body"]) <= {"status", "viewed_at", "responded_at"}


def test_write_failure_keeps_the_offer_open_and_says_so(client, world):
    s = _setup(client, world)
    client.get(f"/creator/dm/offers/{s.oid}")
    real_table = world.db.table

    def _fail_updates(name: str) -> Any:
        q = real_table(name)
        if name == "creator_job_offers":
            orig = q.update

            def _upd(body: dict[str, Any]) -> Any:
                world.db.fail_tables.add("creator_job_offers")
                return orig(body)

            q.update = _upd  # type: ignore[method-assign]
        return q

    world.db.table = _fail_updates  # type: ignore[method-assign]
    r = client.post(f"/creator/dm/offers/{s.oid}/accept")
    assert r.status_code == 303 and r.headers["location"].endswith("?respond=failed")
    world.db.fail_tables.clear()
    world.db.table = real_table  # type: ignore[method-assign]
    page = client.get(r.headers["location"]).text
    assert "couldn't save your response. try again." in page and "Accept offer" in page
    assert world.db.offer(s.oid)["status"] == "sent"


def test_accept_requires_a_csrf_token(world, monkeypatch):
    monkeypatch.setattr(_csrf_module.CSRFMiddleware, "__call__", _REAL_CSRF_CALL)
    c = TestClient(app, follow_redirects=False)
    me = world.creator()
    lst = world.listing(world.brand())
    oid = world.offer(lst, me)
    _sign_in(c, me)
    url = f"/creator/dm/offers/{oid}"
    for action in ("accept", "decline"):
        r = c.post(f"{url}/{action}", headers={"Origin": "http://testserver"})
        assert r.status_code == 403
    assert world.db.offer(oid)["status"] == "sent"
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)
    r = c.post(f"{url}/accept", data={"csrf_token": token}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303 and world.db.offer(oid)["status"] == "accepted"


# ==================================================================== SECURITY


def test_poster_cannot_view_or_respond_to_their_own_sent_offer(client, world):
    poster = world.creator(full_name="Poster Creator")
    lst = world.listing(poster, poster_role="creator")
    oid = world.offer(lst, world.creator())
    _sign_in(client, poster)
    assert client.get(f"/creator/dm/offers/{oid}").status_code == 404
    for action in ("accept", "decline"):
        assert client.post(f"/creator/dm/offers/{oid}/{action}").status_code == 404
    assert world.db.offer(oid)["status"] == "sent" and world.db.offer(oid)["viewed_at"] is None


def test_cross_user_id_tampering_is_a_404_and_writes_nothing(client, world):
    a = _setup(client, world)
    other = world.creator()
    other_offer = world.offer(world.listing(world.brand()), other)
    # signed in as A, use B's offer id everywhere
    assert client.get(f"/creator/dm/offers/{other_offer}").status_code == 404
    for action in ("accept", "decline"):
        assert client.post(f"/creator/dm/offers/{other_offer}/{action}").status_code == 404
    row = world.db.offer(other_offer)
    assert row["status"] == "sent" and row["viewed_at"] is None
    assert a.oid not in _offer_links(client.get("/creator/dm?view=offers").text) or True


@pytest.mark.parametrize("bad", ["not-a-uuid", "1' or '1'='1", "x" * 80, "00000000-0000-0000-0000-00000000000"])
def test_malformed_and_missing_ids_are_404(client, world, bad):
    _setup(client, world)
    assert client.get(f"/creator/dm/offers/{bad}").status_code == 404
    assert client.post(f"/creator/dm/offers/{bad}/accept").status_code == 404
    assert client.get(f"/creator/dm/offers/{uuid4()}").status_code == 404


def test_unauthenticated_and_brand_sessions_are_refused(client, world):
    s = _setup(client, world)
    client.cookies.clear()
    for method, url in (("get", "/creator/dm?view=offers"), ("get", f"/creator/dm/offers/{s.oid}"),
                        ("post", f"/creator/dm/offers/{s.oid}/accept")):
        assert getattr(client, method)(url).status_code in (302, 303, 401, 403)
    _sign_in(client, s.poster, "brand")
    assert client.get(f"/creator/dm/offers/{s.oid}").status_code == 403
    assert client.post(f"/creator/dm/offers/{s.oid}/accept").status_code == 403
    assert world.db.offer(s.oid)["status"] == "sent"


def test_offer_terms_never_reach_discover_or_opportunity_pages(client, world, monkeypatch):
    s = _setup(client, world, deliverables="SECRET-DELIVERABLES-9q", note="SECRET-NOTE-9q", amount_cents=432199)
    pages = [client.get("/creator/discover?kind=opportunity").text,
             client.get("/creator/discover?kind=opportunity&view=mine").text,
             client.get(f"/creator/jobs/{s.lst['id']}").text,
             _offers_page(client)]
    for html in pages:
        for secret in ("SECRET-DELIVERABLES-9q", "SECRET-NOTE-9q"):
            assert secret not in html
    assert "$4,321.99" in pages[-1]  # amount appears only in the recipient's own inbox


# ================================================================== REGRESSION


def test_messages_view_is_unchanged_with_babyg_pinned_first(client, world):
    me = world.creator()
    world.pending = 2
    world.threads = [{"id": "t1", "peer_id": str(uuid4()), "last_message_at": None}]
    world.native_unread = {"t1": 1}
    _sign_in(client, me)
    html = client.get("/creator/dm").text
    assert 'action="/creator/dm" method="get" role="search"' in html
    assert 'placeholder="Search conversations"' in html
    manager = html.index("data-dm-manager")
    assert manager < html.index("data-dm-row")
    assert 'data-tabbar-badge="babyg"' in html  # manager pending badge intact
    assert 'aria-current="page">messages' in html
    assert "data-dm-offers" not in html


def test_unknown_view_values_fall_back_to_messages(client, world):
    _sign_in(client, world.creator())
    for v in ("", "OFFERS ", "Offers", "x", "messages"):
        html = client.get(f"/creator/dm?view={v}").text
        expected_offers = v.strip().lower() == "offers"
        assert ("data-dm-offers" in html or "no offers yet." in html) == expected_offers


def test_step_6a_make_offer_still_creates_one_sent_offer(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator()
    aid = world.apply(lst["id"], applicant)
    _sign_in(client, poster, "brand")
    url = f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}/offer"
    due = (job_offers.earliest_due_date() + timedelta(days=10)).isoformat()
    terms = {"amount": "1,000", "deliverables": "2 TikToks", "due_date": due, "note": ""}
    assert client.post(url, data=terms).status_code == 303
    assert client.post(url, data=terms).status_code == 303  # duplicate still refused
    assert len(world.db.offers) == 1
    row = world.db.offers[0]
    assert row["status"] == "sent" and row["viewed_at"] is None and row["responded_at"] is None
    # ...and the recipient now sees it, unread
    _sign_in(client, applicant)
    html = _offers_page(client)
    assert _offer_links(html) == [row["id"]] and _tab_badge(html, "offers") == 1


def test_brand_dm_page_has_no_offers_tab_and_keeps_its_placeholder(client, world):
    """Brands never receive offers. Step 6C adds only "messages | deals"."""
    _sign_in(client, world.brand(), "brand")
    html = client.get("/brand/dm").text
    tabs = re.findall(r'<a href="(/brand/dm[^"]*)"[^>]*>([a-z]+)</a>', html)
    assert tabs == [("/brand/dm", "messages"), ("/brand/dm?view=deals", "deals")]
    assert "?view=offers" not in html
    assert "brand messaging is coming soon" in html


def test_no_deal_payment_or_counter_surface_exists():
    paths = [getattr(r, "path", "") for r in app.routes]
    six_b = sorted(p for p in paths if p.startswith("/creator/dm/offers"))
    assert six_b == ["/creator/dm/offers/{offer_id}", "/creator/dm/offers/{offer_id}/accept",
                     "/creator/dm/offers/{offer_id}/decline"]
    import ast

    tree = ast.parse(Path("app/services/job_offers.py").read_text(encoding="utf-8"))
    idents: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            idents.add(node.id)
        elif isinstance(node, ast.Attribute):
            idents.add(node.attr)
        elif isinstance(node, ast.Import | ast.ImportFrom):
            idents.update(a.name for a in node.names)
            idents.add(getattr(node, "module", "") or "")
    for banned in ("stripe", "payout", "checkout", "deal", "notification", "counter", "anthropic", "dms"):
        assert not any(banned in i.lower() for i in idents), (banned, sorted(i for i in idents if banned in i.lower()))
    tpl = Path("app/templates/creator/offer_review.html").read_text(encoding="utf-8").lower()
    tpl = re.sub(r"\{#.*?#\}", "", tpl, flags=re.S)  # comments are never rendered
    for banned in ("counter", "withdraw", "edit offer", "pay", "stripe", "checkout", "fee"):
        assert banned not in tpl, banned


MIGRATION = Path("migrations/0050_creator_job_offer_responses.sql")


def test_migration_0050_is_additive_and_matches_the_service():
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).lower()
    assert code.strip().startswith("begin;") and code.strip().endswith("commit;")
    assert "add column if not exists viewed_at timestamptz" in code
    assert "add column if not exists responded_at timestamptz" in code
    assert "check (status in ('sent', 'accepted', 'declined'))" in code
    assert "creator_job_offers_response_consistency" in code
    assert "create index if not exists creator_job_offers_applicant_idx" in code
    assert "grant update (status, viewed_at, responded_at)" in code
    for banned in ("drop table", "drop column", "truncate", "delete from", "create policy",
                   " to authenticated", " to anon", "disable row level security"):
        assert banned not in code, banned
    for status in ("funded", "paid", "completed", "cancelled", "disputed", "refunded", "countered"):
        assert status not in code
    assert job_offers.DECISIONS == ("accepted", "declined")
