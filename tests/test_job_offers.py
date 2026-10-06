"""Step 6A: the poster of an opportunity makes ONE offer for one application.

Flow under test:

    Application -> Make offer -> (form) -> Send offer -> Offer sent

Rules proven here:

* only the listing's poster (``listing.poster_user_id`` == session user) can
  open the form or create the offer, and only for an application that
  belongs to that listing; never for a self-relationship;
* the browser supplies ONLY the four terms (amount, deliverables, due date,
  note); listing / poster / applicant / currency / status are server-owned;
* money is integer cents, never floating point;
* at most one offer per application (service pre-check + DB unique);
* nothing beyond "Offer sent": no deal, payment, Stripe, notification,
  accept/decline/counter.

These tests run the REAL ``jobs``, ``profiles``, ``job_applications`` and
``job_offers`` services against an in-memory PostgREST fake that honors
filters/projection and emulates migration 0049's constraints, so the actual
queries and writes are exercised. Unknown tables fail like the unconfigured
production client, as in the other route tests.
"""

from __future__ import annotations

import hashlib
import importlib
import re
from datetime import UTC, date, datetime, timedelta
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

# Snapshot the real CSRF dispatch before conftest's autouse fixture patches it.
_REAL_CSRF_CALL = _csrf_module.CSRFMiddleware.__call__

from app.main import app  # noqa: E402
from app.services import discover as discover_service  # noqa: E402
from app.services import discovery, job_offers, network, views  # noqa: E402

SECRET_MESSAGE = "APPLICATION-MESSAGE-7c1d"
SECRET_DELIVERABLES = "SECRET-DELIVERABLES-2-TikToks-1-Reel"
SECRET_NOTE = "SECRET-NOTE-ship-by-friday"
WHEN = "2026-03-04T10:30:00Z"

_TABLES = (
    "creator_job_listings",
    "creator_job_applications",
    "creator_profiles",
    "brand_profiles",
    "creator_job_offers",
)


def _api_error(code: str, msg: str) -> PostgrestAPIError:
    return PostgrestAPIError({"message": msg, "code": code, "hint": None, "details": None})


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

    def _check_offer(self, body: dict[str, Any]) -> None:
        """Emulate migration 0049's constraints."""
        t = self.db.tables
        if any(r["application_id"] == body.get("application_id") for r in t["creator_job_offers"]):
            raise _api_error("23505", "creator_job_offers_one_per_application")
        amount = body.get("amount_cents")
        deliverables = body.get("deliverables")
        note = body.get("note")
        if not isinstance(amount, int) or not (0 < amount <= 1_000_000_000):
            raise _api_error("23514", "creator_job_offers_amount_cents_check")
        if body.get("currency", "USD") != "USD":
            raise _api_error("23514", "creator_job_offers_currency_check")
        if body.get("status", "sent") != "sent":
            raise _api_error("23514", "creator_job_offers_status_check")
        if not deliverables or len(deliverables) > 2000 or not deliverables.strip():
            raise _api_error("23514", "creator_job_offers_deliverables_check")
        if note is not None and len(note) > 2000:
            raise _api_error("23514", "creator_job_offers_note_check")
        if not body.get("due_date"):
            raise _api_error("23502", "due_date not null")
        if body.get("poster_user_id") == body.get("applicant_user_id"):
            raise _api_error("23514", "creator_job_offers_not_self")
        if not any(r["id"] == body.get("application_id") for r in t["creator_job_applications"]):
            raise _api_error("23503", "application fk")
        if not any(r["id"] == body.get("listing_id") for r in t["creator_job_listings"]):
            raise _api_error("23503", "listing fk")
        if not any(r["user_id"] == body.get("applicant_user_id") for r in t["creator_profiles"]):
            raise _api_error("23503", "applicant fk")

    def execute(self) -> SimpleNamespace:
        self.db.queries.append(
            {"table": self.name, "op": self.op, "cols": self.cols, "eqs": list(self.eqs), "body": self.body}
        )
        if self.name in self.db.fail_tables:
            raise _api_error("500", "boom")
        rows = self.db.tables[self.name]
        if self.op == "insert":
            assert self.body is not None
            if self.name == "creator_job_offers":
                self._check_offer(self.body)
            if self.name == "creator_job_applications" and any(
                r["listing_id"] == self.body["listing_id"]
                and r["applicant_user_id"] == self.body["applicant_user_id"]
                for r in rows
            ):
                raise _api_error("23505", "duplicate application")
            row = {"id": str(uuid4()), "created_at": WHEN, **self.body}
            if self.name == "creator_job_offers":
                row.setdefault("currency", "USD")
                row.setdefault("status", "sent")
            if self.name == "creator_job_applications":
                row.setdefault("status", "submitted")
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

    @property
    def offers(self) -> list[dict[str, Any]]:
        return self.tables["creator_job_offers"]

    def inserts(self) -> list[str]:
        return [q["table"] for q in self.queries if q["op"] == "insert"]


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
            "profile_photo_url": None,
            "onboarding_completed_at": "2026-01-01T00:00:00Z",
            "niches": ["fashion"],
            "content_formats": [],
            "hard_limits": [],
            "primary_platform": "Instagram",
            "bio": "Short bio.",
        }
        row.update(kw)
        self.db.tables["creator_profiles"].append(row)
        return uid

    def brand(self, uid: str | None = None, **kw: Any) -> str:
        uid = uid or str(uuid4())
        row = {
            "user_id": uid,
            "company_name": "Olipop",
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
            "id": str(uuid4()),
            "poster_user_id": poster,
            "poster_role": kw.pop("poster_role", "brand"),
            "title": kw.pop("title", "Autumn campaign reels"),
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

    def apply(self, listing_id: str, applicant: str, message: str = SECRET_MESSAGE) -> str:
        aid = str(uuid4())
        self.db.tables["creator_job_applications"].append(
            {
                "id": aid,
                "listing_id": listing_id,
                "applicant_user_id": applicant,
                "message": message,
                "status": "submitted",
                "created_at": WHEN,
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
    monkeypatch.setattr(network, "get_connection_between", lambda a, b: None)
    monkeypatch.setattr(views, "record_view", lambda **kw: True)
    monkeypatch.setattr(discovery, "record_action", lambda **kw: True)
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(discover_service, "record_action", lambda **kw: True)
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [])

    def _creator_card(uid: str) -> dict[str, Any] | None:
        if uid not in w.discoverable:
            return None
        return {
            "card_kind": "creator", "card_id": uid, "owner_user_id": uid, "title": "Sam Rivera",
            "subtitle": "@samrivera", "image_url": None, "location_label": None, "tags": [],
            "description": "Short bio.", "relevance_reasons": [], "detail_path": f"/creator/network/{uid}",
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
                    "card_kind": "opportunity", "card_id": r["id"], "owner_user_id": r["poster_user_id"],
                    "title": r["title"], "subtitle": "Olipop", "location_label": "Austin, TX",
                    "tags": ["fashion"], "description": r["description"],
                    "compensation_text": r["compensation_text"], "budget_min": None,
                    "budget_max": None, "deadline": None, "detail_path": f"/creator/jobs/{r['id']}",
                }
        return out

    monkeypatch.setattr(discover_service, "get_opportunity_cards", _opportunity_cards)
    return w


@pytest.fixture(autouse=True)
def no_side_effect_systems(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any call into payments, Stripe, deals, DMs or notifications fails the test."""
    guarded = {
        "app.services.stripe_client": None,
        "app.integrations.stripe_client": None,
        "app.services.creator_payouts": None,
        "app.services.babyg_deals": None,
        "app.services.deal_manager": None,
        "app.services.instagram_dms": None,
        "app.services.notifications": {"create"},
        "app.services.dms": {"send_message", "create_thread", "get_or_create_thread", "send"},
    }
    for mod_name, only in guarded.items():
        try:
            mod = importlib.import_module(mod_name)
        except ImportError:
            continue
        for attr in dir(mod):
            if attr.startswith("_") or not callable(getattr(mod, attr, None)):
                continue
            if isinstance(getattr(mod, attr), type):
                continue
            if only is not None and attr not in only:
                continue

            def _boom(*a: Any, _m: str = mod_name, _a: str = attr, **k: Any) -> None:
                raise AssertionError(f"Step 6A touched {_m}.{_a}")

            monkeypatch.setattr(mod, attr, _boom, raising=False)
    try:
        import stripe

        def _no_stripe(*a: Any, **k: Any) -> None:
            raise AssertionError("Step 6A constructed a Stripe client")

        monkeypatch.setattr(stripe, "StripeClient", _no_stripe, raising=False)
    except ImportError:
        pass


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, uid: str, role: str) -> str:
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])
    return uid


ROLES = pytest.mark.parametrize("role", ["brand", "creator"])


def _base(role: str, lid: str) -> str:
    return f"/brand/discover/opportunity/{lid}" if role == "brand" else f"/creator/jobs/{lid}"


def _app_url(role: str, lid: str, aid: str) -> str:
    return f"{_base(role, lid)}/applicants/{aid}"


def _offer_url(role: str, lid: str, aid: str) -> str:
    return f"{_app_url(role, lid, aid)}/offer"


def _setup(client, world, role: str, **app_kw: Any) -> SimpleNamespace:
    """A poster (signed in) with one listing and one applicant."""
    me = world.brand() if role == "brand" else world.creator(full_name="Poster Person")
    _sign_in(client, me, role)
    lst = world.listing(me, poster_role=role)
    applicant = world.creator(full_name=app_kw.pop("full_name", "Alex Kim"), instagram_handle="alexk")
    aid = world.apply(lst["id"], applicant)
    return SimpleNamespace(me=me, lst=lst, applicant=applicant, aid=aid, role=role)


def _future(days: int = 30) -> str:
    return (job_offers.earliest_due_date() + timedelta(days=days)).isoformat()


def _terms(**kw: Any) -> dict[str, str]:
    data = {"amount": "1,000", "deliverables": "2 TikToks\n1 Instagram Reel", "due_date": _future(), "note": ""}
    data.update(kw)
    return data


def _form_names(html: str) -> set[str]:
    """Field names of the Make offer form only (the shell may hold other forms)."""
    start = html.index('class="op-new-form"')
    form = html[start : html.index("</form>", start)]
    return set(re.findall(r'name="([^"]+)"', form))


# ============================================================ the Make offer page


@ROLES
def test_poster_can_open_make_offer_for_their_own_applicant(client, world, role):
    s = _setup(client, world, role)
    r = client.get(_offer_url(role, s.lst["id"], s.aid))
    assert r.status_code == 200
    html = r.text
    assert '<h1 class="detail-title">Make offer</h1>' in html
    assert s.lst["title"] in html and "Alex Kim" in html  # compact who + what
    assert 'for="offer-amount">Offer amount <span class="op-new-optional">USD</span>' in html
    assert 'for="offer-deliverables">Deliverables</label>' in html
    assert 'for="offer-due-date">Due date</label>' in html
    assert 'for="offer-note">Note <span class="op-new-optional">(optional)</span>' in html
    assert '<button type="submit" class="btn btn-lime">Send offer</button>' in html
    assert f'action="{_offer_url(role, s.lst["id"], s.aid)}"' in html
    assert f'<a href="{_app_url(role, s.lst["id"], s.aid)}" class="back-link">← application</a>' in html
    assert 'type="date" name="due_date"' in html and 'inputmode="decimal"' in html
    # the browser submits ONLY the four terms (+ the CSRF token)
    assert _form_names(html) == {"csrf_token", "amount", "deliverables", "due_date", "note"}
    # the applicant's private message is not repeated on this page
    assert SECRET_MESSAGE not in html


@ROLES
def test_non_poster_cannot_open_or_submit(client, world, role):
    s = _setup(client, world, role)
    intruder = world.brand() if role == "brand" else world.creator()
    _sign_in(client, intruder, role)
    assert client.get(_offer_url(role, s.lst["id"], s.aid)).status_code == 403
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms())
    assert r.status_code == 403
    assert world.db.offers == []


def test_applicant_cannot_create_an_offer_for_themselves(client, world):
    s = _setup(client, world, "brand")
    _sign_in(client, s.applicant, "creator")
    url = f"/creator/jobs/{s.lst['id']}/applicants/{s.aid}/offer"
    assert client.get(url).status_code == 403
    assert client.post(url, data=_terms()).status_code == 403
    assert world.db.offers == []


def test_other_applicants_cannot_open_or_submit(client, world):
    s = _setup(client, world, "brand")
    other_applicant = world.creator()
    world.apply(s.lst["id"], other_applicant)
    _sign_in(client, other_applicant, "creator")
    url = f"/creator/jobs/{s.lst['id']}/applicants/{s.aid}/offer"
    assert client.get(url).status_code == 403
    assert client.post(url, data=_terms()).status_code == 403
    assert world.db.offers == []


@ROLES
def test_unrelated_user_and_anonymous_cannot_open_or_submit(client, world, role):
    s = _setup(client, world, role)
    for intruder_role in ("brand", "creator"):
        intruder = world.brand() if intruder_role == "brand" else world.creator()
        _sign_in(client, intruder, intruder_role)
        for url in (_offer_url(role, s.lst["id"], s.aid), f"{_offer_url(role, s.lst['id'], s.aid)}/sent"):
            assert client.get(url).status_code == 403
        assert client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms()).status_code == 403
    client.cookies.clear()
    assert client.get(_offer_url(role, s.lst["id"], s.aid)).status_code in (302, 303, 401, 403)
    assert client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms()).status_code in (
        302, 303, 401, 403,
    )
    assert world.db.offers == []


@ROLES
def test_listing_application_mismatch_fails(client, world, role):
    s = _setup(client, world, role)
    other_listing = world.listing(s.me, poster_role=role, title="Second")
    other_aid = world.apply(other_listing["id"], world.creator())
    # application of listing B under listing A (both mine) -> 404, nothing written
    url = _offer_url(role, s.lst["id"], other_aid)
    assert client.get(url).status_code == 404
    assert client.post(url, data=_terms()).status_code == 404
    # an application of SOMEONE ELSE's listing under my listing -> 404
    foreign = world.listing(world.brand(), title="Not mine")
    foreign_aid = world.apply(foreign["id"], world.creator())
    assert client.post(_offer_url(role, s.lst["id"], foreign_aid), data=_terms()).status_code == 404
    # ...and under their listing -> 403 (not the poster)
    assert client.post(_offer_url(role, foreign["id"], foreign_aid), data=_terms()).status_code == 403
    assert world.db.offers == []


@ROLES
@pytest.mark.parametrize("bad", ["not-a-uuid", "1' or '1'='1", "..%2f..%2fetc", "x" * 80])
def test_malformed_ids_fail_safely(client, world, role, bad):
    s = _setup(client, world, role)
    for url in (_offer_url(role, bad, s.aid), _offer_url(role, s.lst["id"], bad)):
        assert client.get(url).status_code in (404, 422)
        assert client.post(url, data=_terms()).status_code in (404, 422)
    assert world.db.offers == []


@ROLES
def test_missing_listing_and_missing_application_fail_safely(client, world, role):
    s = _setup(client, world, role)
    for url in (_offer_url(role, str(uuid4()), s.aid), _offer_url(role, s.lst["id"], str(uuid4()))):
        assert client.get(url).status_code == 404
        assert client.post(url, data=_terms()).status_code == 404
    assert world.db.offers == []


@ROLES
def test_taken_down_listing_cannot_receive_an_offer(client, world, role):
    s = _setup(client, world, role)
    world.db.tables["creator_job_listings"][-1]["is_taken_down"] = True
    assert client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms()).status_code == 404
    assert world.db.offers == []


@ROLES
def test_self_relationship_is_refused_and_not_fixed(client, world, role):
    me = world.brand() if role == "brand" else world.creator()
    if role == "brand":
        world.creator(uid=me)  # a malformed row: the poster also has a creator profile
    _sign_in(client, me, role)
    lst = world.listing(me, poster_role=role)
    aid = world.apply(lst["id"], me)  # corrupt data: poster == applicant
    assert client.get(_offer_url(role, lst["id"], aid)).status_code == 403
    assert client.post(_offer_url(role, lst["id"], aid), data=_terms()).status_code == 403
    page = client.get(_app_url(role, lst["id"], aid))
    assert page.status_code == 200 and "Make offer" not in page.text
    assert world.db.offers == []
    assert world.db.tables["creator_job_applications"][-1]["applicant_user_id"] == me  # untouched


# ======================================================== server-derived identity


@ROLES
def test_offer_identities_are_derived_server_side_and_spoofs_ignored(client, world, role):
    s = _setup(client, world, role)
    attacker = str(uuid4())
    other_listing = world.listing(world.brand())
    spoof = _terms(
        applicant_user_id=attacker,
        poster_user_id=attacker,
        listing_id=other_listing["id"],
        application_id=str(uuid4()),
        currency="EUR",
        status="accepted",
        amount_cents="1",
        role="brand",
    )
    r = client.post(
        _offer_url(role, s.lst["id"], s.aid),
        data=spoof,
        params={"poster_user_id": attacker, "applicant_user_id": attacker, "user_id": attacker},
        headers={"X-User-Id": attacker, "X-Role": "brand"},
    )
    assert r.status_code == 303
    [offer] = world.db.offers
    assert offer["applicant_user_id"] == s.applicant       # from the application row
    assert offer["poster_user_id"] == s.me                  # from session == listing.poster
    assert offer["listing_id"] == s.lst["id"]              # from the listing row
    assert offer["application_id"] == s.aid
    assert offer["currency"] == "USD"                       # server constant
    assert offer["status"] == "sent"                        # server constant
    assert offer["amount_cents"] == 100000                  # parsed from "amount", not amount_cents
    insert = next(q for q in world.db.queries if q["op"] == "insert")
    assert set(insert["body"]) == {
        "application_id", "listing_id", "poster_user_id", "applicant_user_id",
        "amount_cents", "currency", "deliverables", "due_date", "note", "status",
    }


def test_spoofed_ownership_cannot_create_on_someone_elses_listing(client, world):
    owner = world.brand()
    lst = world.listing(owner)
    aid = world.apply(lst["id"], world.creator())
    me = world.brand()
    _sign_in(client, me, "brand")
    r = client.post(
        f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}/offer",
        data=_terms(poster_user_id=me, owner=me, listing_owner=me),
        params={"poster_user_id": me},
    )
    assert r.status_code == 403
    assert world.db.offers == []


# =================================================================== amounts


@ROLES
@pytest.mark.parametrize(
    ("raw", "cents"),
    [
        ("1000", 100000),
        ("1,000", 100000),
        ("$1,000.00", 100000),
        ("250.50", 25050),
        ("250.5", 25050),
        ("0.01", 1),
        ("10,000,000.00", 1_000_000_000),  # the anti-abuse ceiling itself is allowed
    ],
)
def test_valid_amounts_are_stored_as_exact_integer_cents(client, world, role, raw, cents):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(amount=raw))
    assert r.status_code == 303, r.text[:300]
    [offer] = world.db.offers
    assert offer["amount_cents"] == cents
    assert isinstance(offer["amount_cents"], int) and not isinstance(offer["amount_cents"], bool)


@ROLES
@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "Enter an offer amount."),
        ("0", "Offer amount must be greater than $0."),
        ("0.00", "Offer amount must be greater than $0."),
        ("-5", "Offer amount must be greater than $0."),
        ("abc", "Enter a valid amount, like 1,000.00."),
        ("1e3", "Enter a valid amount, like 1,000.00."),
        ("NaN", "Enter a valid amount, like 1,000.00."),
        ("Infinity", "Enter a valid amount, like 1,000.00."),
        ("1,00", "Enter a valid amount, like 1,000.00."),
        ("12 34", "Enter a valid amount, like 1,000.00."),
        ("١٢٣", "Enter a valid amount, like 1,000.00."),
        ("1.234", "Offer amount can have at most 2 decimal places."),
        ("10000000.01", "Offer amount can&#39;t be more than $10,000,000.00."),
        ("99999999999", "Offer amount can&#39;t be more than $10,000,000.00."),
        ("9" * 40, "Offer amount can&#39;t be more than $10,000,000.00."),
    ],
)
def test_invalid_amounts_are_rejected_on_the_amount_field(client, world, role, raw, message):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(amount=raw, note="keep me"))
    assert r.status_code == 400
    assert world.db.offers == []
    html = r.text
    assert '<h1 class="detail-title">Make offer</h1>' in html  # stayed on the form
    assert f'<span class="op-new-banner" id="offer-amount-error">{message}</span>' in html
    assert 'aria-invalid="true" aria-describedby="offer-amount-error"' in html
    # valid fields are preserved
    assert ">2 TikToks\n1 Instagram Reel</textarea>" in html
    assert ">keep me</textarea>" in html
    assert f'value="{_future()}"' in html


def test_amount_parsing_never_uses_floating_point():
    src = Path("app/services/job_offers.py").read_text(encoding="utf-8")
    assert "float(" not in src and "Decimal" not in src
    assert job_offers.parse_amount_cents("0.29") == (29, None)  # 0.29 * 100 != 29 in float
    assert job_offers.parse_amount_cents("1.15") == (115, None)


# ==================================================================== text fields


@ROLES
@pytest.mark.parametrize("value", ["", "   ", "\n\n"])
def test_empty_deliverables_rejected(client, world, role, value):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(deliverables=value))
    assert r.status_code == 400 and world.db.offers == []
    assert 'id="offer-deliverables-error">Describe the deliverables.</span>' in r.text
    assert 'value="1,000"' in r.text  # amount preserved


@ROLES
def test_deliverables_length_limit(client, world, role):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(deliverables="d" * 2001))
    assert r.status_code == 400 and world.db.offers == []
    assert "Deliverables must be 2,000 characters or fewer." in r.text
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(deliverables="d" * 2000))
    assert r.status_code == 303
    assert len(world.db.offers[0]["deliverables"]) == 2000


def test_deliverables_keep_line_breaks_and_count_crlf_as_one(client, world):
    s = _setup(client, world, "brand")
    text = "2 TikToks\r\n1 Instagram Reel\r\n" + "x" * 1970  # 2000 visible chars with CRLF
    r = client.post(_offer_url("brand", s.lst["id"], s.aid), data=_terms(deliverables=text))
    assert r.status_code == 303
    assert world.db.offers[0]["deliverables"].startswith("2 TikToks\n1 Instagram Reel\n")


@ROLES
def test_note_is_optional_and_stored_as_null_when_empty(client, world, role):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(note="   "))
    assert r.status_code == 303
    assert world.db.offers[0]["note"] is None


@ROLES
def test_note_length_limit(client, world, role):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(note="n" * 2001))
    assert r.status_code == 400 and world.db.offers == []
    assert 'id="offer-note-error">Note must be 2,000 characters or fewer.</span>' in r.text
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(note="n" * 2000))
    assert r.status_code == 303 and len(world.db.offers[0]["note"]) == 2000


# ======================================================================= due date


@ROLES
@pytest.mark.parametrize("days", [0, 1, 365])
def test_valid_due_dates_accepted(client, world, role, days):
    s = _setup(client, world, role)
    due = _future(days)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(due_date=due))
    assert r.status_code == 303
    assert world.db.offers[0]["due_date"] == due


@ROLES
@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("", "Choose a due date."),
        ("2026-02-30", "Enter a valid due date."),
        ("10/30/2026", "Enter a valid due date."),
        ("20261030", "Enter a valid due date."),
        ("tomorrow", "Enter a valid due date."),
        ("2026-10-30T10:00", "Enter a valid due date."),
    ],
)
def test_missing_or_malformed_due_date_rejected(client, world, role, raw, message):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(due_date=raw))
    assert r.status_code == 400 and world.db.offers == []
    assert f'id="offer-due-date-error">{message}</span>' in r.text


@ROLES
def test_past_due_date_rejected(client, world, role):
    s = _setup(client, world, role)
    past = (job_offers.earliest_due_date() - timedelta(days=1)).isoformat()
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(due_date=past))
    assert r.status_code == 400 and world.db.offers == []
    assert "Due date can&#39;t be in the past." in r.text


def test_earliest_due_date_is_today_somewhere_and_is_the_input_min(client, world):
    late_utc = datetime(2026, 10, 5, 3, 0, tzinfo=UTC)  # still Oct 4 in the Americas
    assert job_offers.earliest_due_date(late_utc) == date(2026, 10, 4)
    assert job_offers.earliest_due_date(datetime(2026, 10, 5, 13, 0, tzinfo=UTC)) == date(2026, 10, 5)
    s = _setup(client, world, "brand")
    html = client.get(_offer_url("brand", s.lst["id"], s.aid)).text
    assert f'min="{job_offers.earliest_due_date().isoformat()}"' in html


@ROLES
def test_all_errors_reported_at_once_and_each_field_identified(client, world, role):
    s = _setup(client, world, role)
    r = client.post(
        _offer_url(role, s.lst["id"], s.aid),
        data={"amount": "0", "deliverables": "", "due_date": "nope", "note": "n" * 2001},
    )
    assert r.status_code == 400 and world.db.offers == []
    for field in ("amount", "deliverables", "due-date", "note"):
        assert f'id="offer-{field}-error"' in r.text
    assert r.text.count('aria-invalid="true"') == 4


# ============================================================ one offer per app


@ROLES
def test_success_redirects_then_offer_sent_page(client, world, role):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms())
    assert r.status_code == 303
    assert r.headers["location"] == f"{_offer_url(role, s.lst['id'], s.aid)}/sent"
    sent = client.get(r.headers["location"])
    assert sent.status_code == 200
    assert '<h1 class="detail-title">Offer sent</h1>' in sent.text
    assert f"Your offer to Alex Kim for {s.lst['title']} was sent." in sent.text
    assert f'href="{_app_url(role, s.lst["id"], s.aid)}" class="btn btn-lime">View application</a>' in sent.text
    # the terms themselves are not echoed anywhere after sending
    assert "2 TikToks" not in sent.text and "1,000" not in sent.text


@ROLES
def test_double_post_creates_exactly_one_offer(client, world, role):
    s = _setup(client, world, role)
    url = _offer_url(role, s.lst["id"], s.aid)
    first = client.post(url, data=_terms(amount="1,000"))
    second = client.post(url, data=_terms(amount="9,999"))
    assert first.status_code == second.status_code == 303
    assert second.headers["location"] == _app_url(role, s.lst["id"], s.aid)
    assert len(world.db.offers) == 1
    assert world.db.offers[0]["amount_cents"] == 100000  # the first one, unchanged


@ROLES
def test_refreshing_the_sent_page_never_duplicates(client, world, role):
    s = _setup(client, world, role)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms())
    for _ in range(3):
        assert client.get(r.headers["location"]).status_code == 200
    assert len(world.db.offers) == 1


def test_racing_submit_hits_the_db_unique_constraint_and_is_treated_as_duplicate(world, monkeypatch):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator()
    aid = world.apply(lst["id"], applicant)
    application = {"id": aid, "listing_id": lst["id"], "applicant_user_id": applicant}
    terms, errors = job_offers.validate_terms(amount="1", deliverables="x", due_date=_future(), note="")
    assert terms is not None and not errors
    assert job_offers.create(listing=lst, application=application, poster_user_id=poster, terms=terms)[0] == (
        job_offers.CREATED
    )
    # second request's pre-check misses (race) -> INSERT -> unique violation
    real = job_offers.get_for_application
    calls = {"n": 0}

    def _racy(*a: Any, **k: Any) -> Any:
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*a, **k)

    monkeypatch.setattr(job_offers, "get_for_application", _racy)
    outcome, existing = job_offers.create(
        listing=lst, application=application, poster_user_id=poster, terms=terms
    )
    assert outcome == job_offers.DUPLICATE and existing is not None
    assert len(world.db.offers) == 1


@ROLES
def test_existing_offer_turns_make_offer_into_offer_sent(client, world, role):
    s = _setup(client, world, role)
    before = client.get(_app_url(role, s.lst["id"], s.aid)).text
    assert f'<a href="{_offer_url(role, s.lst["id"], s.aid)}" class="btn btn-lime">Make offer</a>' in before
    assert "Offer sent" not in before
    client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms())
    after = client.get(_app_url(role, s.lst["id"], s.aid)).text
    assert (
        '<span class="btn btn-ghost opportunity-apply-done" aria-disabled="true" role="status">✓ Offer sent</span>'
        in after
    )
    assert "Make offer" not in after
    assert SECRET_DELIVERABLES not in after


@ROLES
def test_existing_offer_blocks_a_fresh_creation_flow(client, world, role):
    s = _setup(client, world, role)
    client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms())
    r = client.get(_offer_url(role, s.lst["id"], s.aid))
    assert r.status_code == 303 and r.headers["location"] == _app_url(role, s.lst["id"], s.aid)
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(amount="5"))
    assert r.status_code == 303 and len(world.db.offers) == 1


@ROLES
def test_sent_page_without_an_offer_goes_back_to_the_form(client, world, role):
    s = _setup(client, world, role)
    r = client.get(f"{_offer_url(role, s.lst['id'], s.aid)}/sent")
    assert r.status_code == 303 and r.headers["location"] == _offer_url(role, s.lst["id"], s.aid)


@ROLES
def test_write_failure_stays_on_the_form_without_leaking_internals(client, world, role):
    s = _setup(client, world, role)
    world.db.fail_tables.add("creator_job_offers")
    r = client.post(_offer_url(role, s.lst["id"], s.aid), data=_terms(note="keep me"))
    assert r.status_code == 503
    assert "couldn&#39;t send that offer. try again in a moment." in r.text
    assert "boom" not in r.text and "Traceback" not in r.text and "APIError" not in r.text
    assert ">keep me</textarea>" in r.text
    assert world.db.offers == []


def test_brand_onboarding_gate_applies(client, world):
    me = world.brand(onboarding_completed_at=None)
    _sign_in(client, me, "brand")
    lst = world.listing(me)
    aid = world.apply(lst["id"], world.creator())
    for method, url in (
        ("get", _offer_url("brand", lst["id"], aid)),
        ("post", _offer_url("brand", lst["id"], aid)),
        ("get", f"{_offer_url('brand', lst['id'], aid)}/sent"),
    ):
        r = getattr(client, method)(url, **({"data": _terms()} if method == "post" else {}))
        assert r.status_code == 302 and r.headers["location"] == "/onboarding/brand"
    assert world.db.offers == []


# ============================================================== CSRF protection


def test_offer_post_requires_a_csrf_token(world, monkeypatch):
    monkeypatch.setattr(_csrf_module.CSRFMiddleware, "__call__", _REAL_CSRF_CALL)
    c = TestClient(app, follow_redirects=False)
    me = world.brand()
    _sign_in(c, me, "brand")
    lst = world.listing(me)
    aid = world.apply(lst["id"], world.creator())
    url = _offer_url("brand", lst["id"], aid)
    r = c.post(url, data=_terms(), headers={"Origin": "http://testserver"})
    assert r.status_code == 403 and world.db.offers == []
    token = re.search(r'name="csrf_token" value="([^"]+)"', c.get(url).text).group(1)
    r = c.post(url, data={**_terms(), "csrf_token": token}, headers={"Origin": "http://testserver"})
    assert r.status_code == 303 and len(world.db.offers) == 1


# ===================================================== private terms stay private


@ROLES
def test_offer_terms_never_appear_on_discover_my_opportunities_or_applicants(client, world, role, monkeypatch):
    s = _setup(client, world, role)
    client.post(
        _offer_url(role, s.lst["id"], s.aid),
        data=_terms(amount="4,321.99", deliverables=SECRET_DELIVERABLES, note=SECRET_NOTE),
    )
    assert len(world.db.offers) == 1
    explore_card = {
        "card_kind": "opportunity", "card_id": s.lst["id"], "owner_user_id": s.me,
        "title": s.lst["title"], "subtitle": "Olipop", "tags": [], "description": "x",
        "detail_path": f"/creator/jobs/{s.lst['id']}", "relevance_reasons": [],
    }
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [dict(explore_card)])
    disc = "/brand/discover" if role == "brand" else "/creator/discover"
    pages = [
        client.get(f"{disc}?kind=opportunity&view=mine").text,
        client.get(f"{_base(role, s.lst['id'])}/applicants").text,
        client.get(_app_url(role, s.lst["id"], s.aid)).text,
    ]
    _sign_in(client, world.creator(), "creator")
    pages.append(client.get("/creator/discover?kind=opportunity").text)
    _sign_in(client, world.brand(), "brand")
    pages.append(client.get("/brand/discover?kind=opportunity").text)
    _sign_in(client, s.applicant, "creator")  # the applicant: no review-offer experience yet
    pages.append(client.get(f"/creator/jobs/{s.lst['id']}").text)
    pages.append(client.get("/creator/discover?kind=opportunity&view=mine").text)
    for html in pages:
        for secret in (SECRET_DELIVERABLES, SECRET_NOTE, "4,321.99", "432199"):
            assert secret not in html


def test_offer_state_read_selects_no_terms_and_is_poster_scoped(world, db):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator()
    aid = world.apply(lst["id"], applicant)
    job_offers.get_for_application(aid, poster)
    q = [q for q in db.queries if q["table"] == "creator_job_offers"][-1]
    assert q["cols"] == ["id", "status", "created_at"]
    assert ("application_id", aid) in q["eqs"] and ("poster_user_id", poster) in q["eqs"]


def test_offer_state_is_not_visible_to_a_different_poster_id(world):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator()
    aid = world.apply(lst["id"], applicant)
    application = {"id": aid, "listing_id": lst["id"], "applicant_user_id": applicant}
    terms, _ = job_offers.validate_terms(amount="1", deliverables="x", due_date=_future(), note="")
    job_offers.create(listing=lst, application=application, poster_user_id=poster, terms=terms)
    assert job_offers.get_for_application(aid, poster) is not None
    assert job_offers.get_for_application(aid, world.brand()) is None
    assert job_offers.get_for_application("nope", poster) is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda lst, app, poster: ({**lst, "poster_user_id": str(uuid4())}, app, poster),
        lambda lst, app, poster: (lst, {**app, "listing_id": str(uuid4())}, poster),
        lambda lst, app, poster: (lst, {**app, "applicant_user_id": poster}, poster),
        lambda lst, app, poster: (lst, app, "not-a-uuid"),
        lambda lst, app, poster: ({**lst, "id": "bad"}, app, poster),
    ],
)
def test_service_create_reproves_the_relationship(world, db, mutate):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator()
    aid = world.apply(lst["id"], applicant)
    application = {"id": aid, "listing_id": lst["id"], "applicant_user_id": applicant}
    terms, _ = job_offers.validate_terms(amount="1", deliverables="x", due_date=_future(), note="")
    m_lst, m_app, m_poster = mutate(lst, application, poster)
    assert job_offers.create(listing=m_lst, application=m_app, poster_user_id=m_poster, terms=terms) == (
        job_offers.REFUSED,
        None,
    )
    assert db.offers == [] and "creator_job_offers" not in db.inserts()


def test_service_create_rejects_unvalidated_terms(world, db):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator()
    aid = world.apply(lst["id"], applicant)
    application = {"id": aid, "listing_id": lst["id"], "applicant_user_id": applicant}
    for bad in (
        job_offers.OfferTerms(0, "x", date(2030, 1, 1), None),
        job_offers.OfferTerms(job_offers.MAX_AMOUNT_CENTS + 1, "x", date(2030, 1, 1), None),
        job_offers.OfferTerms(100, "   ", date(2030, 1, 1), None),
        job_offers.OfferTerms(100, "x" * 2001, date(2030, 1, 1), None),
        job_offers.OfferTerms(100, "x", date(2030, 1, 1), "n" * 2001),
    ):
        assert job_offers.create(listing=lst, application=application, poster_user_id=poster, terms=bad)[0] == (
            job_offers.REFUSED
        )
    assert db.offers == []


# ======================================================== existing steps intact


def test_step_5b_apply_then_5d_review_then_6a_offer(client, world):
    poster = world.brand()
    lst = world.listing(poster)
    applicant = world.creator(full_name="Fresh Applicant")
    _sign_in(client, applicant, "creator")
    r = client.post(f"/creator/jobs/{lst['id']}/apply", data={"message": "Pick me."})
    assert r.status_code == 303 and r.headers["location"] == f"/creator/jobs/{lst['id']}/applied"
    detail = client.get(f"/creator/jobs/{lst['id']}")
    assert "✓ Applied" in detail.text  # Step 5B state intact
    _sign_in(client, poster, "brand")
    mine = client.get("/brand/discover?kind=opportunity&view=mine")
    assert f'href="/brand/discover/opportunity/{lst["id"]}/applicants"' in mine.text  # 5C
    assert "1 applicant" in mine.text
    listing_page = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants")  # 5D list
    assert "Fresh Applicant" in listing_page.text
    assert "offer" not in listing_page.text.lower().split("<article", 1)[1].split("</article>")[0]
    aid = world.db.tables["creator_job_applications"][0]["id"]
    app_page = client.get(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}")  # 5D detail
    assert "Pick me." in app_page.text and "← applicants" in app_page.text
    r = client.post(f"/brand/discover/opportunity/{lst['id']}/applicants/{aid}/offer", data=_terms())
    assert r.status_code == 303 and len(world.db.offers) == 1


@ROLES
def test_view_profile_still_first_and_unchanged_next_to_make_offer(client, world, role):
    s = _setup(client, world, role)
    world.discoverable.add(s.applicant)
    html = client.get(_app_url(role, s.lst["id"], s.aid)).text
    profile = (
        f"/brand/discover/creator/{s.applicant}" if role == "brand" else f"/creator/network/{s.applicant}"
    )
    actions = html[html.index('<div class="opportunity-application-actions">') :]
    actions = actions[: actions.index("</div>")]
    assert actions.index(f'href="{profile}" class="btn btn-ghost">View profile</a>') < actions.index(
        "Make offer"
    )
    assert client.get(profile).status_code == 200


@ROLES
def test_application_page_keeps_its_step_5d_content(client, world, role):
    s = _setup(client, world, role)
    html = client.get(_app_url(role, s.lst["id"], s.aid)).text
    assert '<h1 class="detail-title">Application</h1>' in html
    assert SECRET_MESSAGE in html and "Alex Kim" in html and "@alexk" in html
    assert "submitted mar 4, 2026" in html
    assert f'<a href="{_base(role, s.lst["id"])}/applicants" class="back-link">← applicants</a>' in html


def test_applied_opportunity_still_routes_to_the_step_5a_detail(client, world):
    s = _setup(client, world, "brand")
    _sign_in(client, s.applicant, "creator")
    page = client.get("/creator/discover?kind=opportunity&view=mine")
    assert f'href="/creator/jobs/{s.lst["id"]}"' in page.text and "✓ Applied" in page.text


def test_explore_and_profile_link_fixes_are_intact(client, world, monkeypatch):
    poster = world.brand()
    lst = world.listing(poster)
    uid = str(uuid4())
    cards = [
        {"card_kind": "opportunity", "card_id": lst["id"], "owner_user_id": poster, "title": "Opp",
         "subtitle": "Olipop", "tags": [], "description": "x", "relevance_reasons": [],
         "detail_path": f"/creator/jobs/{lst['id']}"},
        {"card_kind": "creator", "card_id": uid, "owner_user_id": uid, "title": "Sam", "subtitle": "@sam",
         "tags": [], "description": "x", "relevance_reasons": [], "detail_path": f"/creator/network/{uid}"},
    ]
    monkeypatch.setattr(discover_service, "list_cards", lambda **kw: [dict(c) for c in cards])
    hits = lambda html: re.findall(r'<a class="discover-card-hit" href="([^"]*)"', html)  # noqa: E731
    _sign_in(client, world.creator(), "creator")
    assert hits(client.get("/creator/discover?kind=all").text) == [
        f"/creator/jobs/{lst['id']}", f"/creator/network/{uid}",
    ]
    _sign_in(client, world.brand(), "brand")
    assert hits(client.get("/brand/discover?kind=all").text) == [
        f"/brand/discover/opportunity/{lst['id']}", f"/brand/discover/creator/{uid}",
    ]


# ===================================================== nothing beyond Step 6A


def test_only_the_offer_table_is_written_no_deal_or_payment_objects(client, world):
    s = _setup(client, world, "brand")
    client.post(_offer_url("brand", s.lst["id"], s.aid), data=_terms())
    assert world.db.inserts() == ["creator_job_offers"]
    assert {q["table"] for q in world.db.queries} <= set(_TABLES)


def test_no_accept_decline_counter_edit_or_withdraw_routes_or_controls(client, world):
    paths = [getattr(r, "path", "") for r in app.routes]
    offer_paths = sorted({p for p in paths if "/offer" in p and "/applicants/" in p})
    assert offer_paths == [
        "/brand/discover/opportunity/{opportunity_id}/applicants/{application_id}/offer",
        "/brand/discover/opportunity/{opportunity_id}/applicants/{application_id}/offer/sent",
        "/creator/jobs/{listing_id}/applicants/{application_id}/offer",
        "/creator/jobs/{listing_id}/applicants/{application_id}/offer/sent",
    ]
    # Step 6B adds exactly the RECIPIENT's accept/decline; nothing else.
    respond_paths = sorted(p for p in paths if re.search(r"/offers?/.*(accept|decline)", p))
    assert respond_paths == [
        "/creator/dm/offers/{offer_id}/accept",
        "/creator/dm/offers/{offer_id}/decline",
    ]
    for p in paths:
        assert not re.search(r"/offers?/.*(counter|withdraw|edit|pay|checkout)", p), p
    s = _setup(client, world, "brand")
    client.post(_offer_url("brand", s.lst["id"], s.aid), data=_terms())
    for url in (_app_url("brand", s.lst["id"], s.aid), f"{_offer_url('brand', s.lst['id'], s.aid)}/sent"):
        article = client.get(url).text.split("<article", 1)[1].split("</article>")[0].lower()
        for word in ("accept", "decline", "counter", "withdraw", "edit offer", "deal", "pay", "stripe", "checkout"):
            assert not re.search(rf"\b{word}\b", article), (word, url)


def test_service_has_no_lifecycle_payment_or_integration_code():
    import ast

    src = Path("app/services/job_offers.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    funcs = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assert funcs == {
        # Step 6A (poster side)
        "_clean_text", "parse_amount_cents", "earliest_due_date", "parse_due_date",
        "validate_terms", "can_offer", "get_for_application", "create",
        # Step 6B (recipient side: inbox, viewed, accept/decline, brief)
        "format_usd", "_short_date", "display_status", "_listings_by_id", "_visible",
        "unread_count", "_poster_identities", "_decorate", "list_received",
        "get_received", "mark_viewed", "respond", "compose_brief",
    }
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [f"{node.module}.{a.name}" for a in node.names]
    for name in imported:
        assert not any(b in name.lower() for b in ("stripe", "payout", "deal", "dms", "notification", "oauth"))


# ==================================================================== migration


MIGRATION = Path("migrations/0049_creator_job_offers.sql")


def test_migration_is_the_next_number_and_0048_is_unchanged():
    names = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert names[-5:] == [
        "0048_creator_job_applications.sql",
        MIGRATION.name,
        "0050_creator_job_offer_responses.sql",  # Step 6B
        "0051_creator_job_deals.sql",  # Step 6C
        "0052_creator_job_deal_payments.sql",  # Step 7A
    ]
    # 0049 is applied in production by hand: it must never change
    digest49 = hashlib.sha256(MIGRATION.read_bytes()).hexdigest()
    assert digest49 == "03811cbc0d77c01e91e67a6020290a87f8985de16c28b27ae98cf117d270b1b4"
    digest = hashlib.sha256(Path("migrations/0048_creator_job_applications.sql").read_bytes()).hexdigest()
    assert digest == "18a5baad545a84ad7a9cf18d525e4090139adbe5c86c453316769c6114b61e1d"


def test_migration_is_additive_private_and_matches_the_service_limits():
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).lower()
    assert "create table if not exists public.creator_job_offers" in code
    assert not re.search(r"\bdelete\s+from\b", code)  # "on delete cascade" is fine
    for banned in ("drop ", "truncate", "alter table public.creator_job_applications",
                   "alter table public.creator_job_listings", "create policy", " to authenticated", " to anon"):
        assert banned not in code, banned
    assert "alter table public.creator_job_offers enable row level security" in code
    assert "revoke all on public.creator_job_offers from anon, authenticated" in code
    assert "grant select, insert on public.creator_job_offers to service_role" in code
    assert "constraint creator_job_offers_one_per_application unique (application_id)" in code
    assert "check (poster_user_id <> applicant_user_id)" in code
    assert f"amount_cents <= {job_offers.MAX_AMOUNT_CENTS}" in code
    assert f"char_length(deliverables) <= {job_offers.MAX_DELIVERABLES_CHARS}" in code
    assert f"char_length(note) <= {job_offers.MAX_NOTE_CHARS}" in code
    assert "check (currency = 'usd')" in code and "check (status = 'sent')" in code
    assert "references public.creator_job_applications(id)" in code
    assert "references public.creator_job_listings(id)" in code
    assert "references public.creator_profiles(user_id)" in code
    assert "amount_cents bigint" in code and "due_date date not null" in code
