"""Step 7A: the payer funds an active deal through Stripe Checkout (sandbox).

Flow under test:

    Offer accepted (6B) -> deal (0051 trigger) -> babyg Manager "awaiting payment"
      -> payer opens Deal -> Pay $1,100 -> Stripe-hosted Checkout (destination
         charge to the recipient's connected account, application fee $200)
      -> signed webhook checkout.session.completed -> payment succeeded and,
         in the same transaction (0052 trigger), the deal is funded
      -> both parties: "Funded / Payment confirmed. Work can begin."
      -> babyg Manager "payment confirmed" for both (deduped)

The in-memory PostgREST fake emulates migrations 0051 + 0052 (deal trigger,
payment composite FK, fee CHECK, one-open-payment index, guard trigger,
funding trigger) and 0040's notifications dedupe index. The fake Stripe
client records every call; webhooks are signed exactly like Stripe signs
them and verified by the real SDK in the real route.
"""

from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError as PostgrestAPIError

from app.config import get_settings
from app.core import csrf as _csrf_module
from app.core import supabase_client
from app.core.security import SESSION_COOKIE, write_session

_REAL_CSRF_CALL = _csrf_module.CSRFMiddleware.__call__

from app.main import app  # noqa: E402
from app.services import (  # noqa: E402
    action_proposals,
    babyg_awareness,
    bot,
    bot_nudges,
    creator_payouts,
    deal_events,
    deal_payments,
    discovery,
    dm_briefs,
    dms,
    instagram_dms,
    manager_activity,
    network,
    notifications,
    views,
)
from app.services import discover as discover_service  # noqa: E402

WEBHOOK_SECRET = "whsec_test_step7a"
_TABLES = ("creator_job_listings", "creator_job_applications", "creator_profiles", "brand_profiles",
           "creator_job_offers", "creator_job_deals", "creator_job_deal_payments", "notifications",
           "creator_payout_accounts")


def _err(code: str, msg: str) -> PostgrestAPIError:
    return PostgrestAPIError({"message": msg, "code": code, "hint": None, "details": None})


def _fee(base: int) -> int:  # the SQL CHECK's formula, independently
    return (base * 1000 + 5000) // 10000


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

    def upsert(self, *a: Any, **k: Any) -> _Query:
        raise AssertionError("upsert on_conflict cannot target the partial notifications index")

    def eq(self, col: str, val: Any) -> _Query:
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col: str, val: Any) -> _Query:
        self.filters.append(("is", col, val))
        return self

    def in_(self, col: str, vals: list[Any]) -> _Query:
        self.filters.append(("in", col, list(vals)))
        return self

    def gte(self, col: str, val: Any) -> _Query:
        self.filters.append(("gte", col, val))
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
            if kind == "gte" and str(r.get(col) or "") < str(val):
                return False
        return True

    def execute(self) -> SimpleNamespace:
        self.db.queries.append({"table": self.name, "op": self.op, "filters": list(self.filters),
                                "body": self.body})
        if self.name in self.db.fail_tables or (self.op, self.name) in self.db.fail_ops:
            raise _err("500", "boom")
        rows = self.db.tables[self.name]
        if self.op == "insert":
            assert self.body is not None
            row = {"id": str(uuid4()), "created_at": self.db.now(), **self.body}
            self.db.check_insert(self.name, row)
            rows.append(row)
            return SimpleNamespace(data=[dict(row)])
        matched = [r for r in rows if self._match(r)]
        if self.op == "update":
            assert self.body is not None
            out = []
            for r in matched:
                before = dict(r)
                cand = {**r, **self.body}
                self.db.check_update(self.name, before, cand)
                r.update(self.body)
                if self.name == "creator_job_deal_payments":
                    r["updated_at"] = self.db.now()
                out.append(dict(r))
                self.db.after_update(self.name, before, r)
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
        self.fail_ops: set[tuple[str, str]] = set()
        self._tick = 0

    def now(self) -> str:
        self._tick += 1
        return (datetime.now(UTC) - timedelta(hours=1) + timedelta(seconds=self._tick)).isoformat()

    def table(self, name: str) -> _Query:
        if name not in self.tables:
            raise RuntimeError(f"unexpected table {name} (test fake)")
        return _Query(self, name)

    @property
    def deals(self) -> list[dict[str, Any]]:
        return self.tables["creator_job_deals"]

    @property
    def payments(self) -> list[dict[str, Any]]:
        return self.tables["creator_job_deal_payments"]

    @property
    def notes(self) -> list[dict[str, Any]]:
        return self.tables["notifications"]

    # --- constraints (0051 / 0052 / 0040) ---------------------------------
    def check_insert(self, name: str, row: dict[str, Any]) -> None:
        if name == "creator_job_deals":
            if any(d["offer_id"] == row["offer_id"] for d in self.deals):
                raise _err("23505", "creator_job_deals_one_per_offer")
            row.setdefault("status", "active")
            row.setdefault("funded_at", None)
        if name == "notifications":
            row.setdefault("archived_at", None)
            row.setdefault("is_read", False)
            if row.get("source_event_id") and any(
                (n["user_id"], n["source_provider"], n["source_event_id"])
                == (row["user_id"], row["source_provider"], row["source_event_id"]) for n in self.notes
            ):
                raise _err("23505", "uq_notifications_source_event")
        if name == "creator_job_deal_payments":
            row.setdefault("status", "pending")
            for k in ("stripe_checkout_session_id", "stripe_payment_intent_id", "succeeded_at",
                      "last_stripe_event_id"):
                row.setdefault(k, None)
            deal = next((d for d in self.deals if d["id"] == row["deal_id"]), None)
            if deal is None or (deal["poster_user_id"], deal["applicant_user_id"], deal["amount_cents"]) != (
                    row["payer_user_id"], row["recipient_user_id"], row["base_amount_cents"]):
                raise _err("23503", "creator_job_deal_payments_deal_terms")
            self._check_payment(row)
            if row["status"] in ("pending", "succeeded") and any(
                    p["deal_id"] == row["deal_id"] and p["status"] in ("pending", "succeeded")
                    for p in self.payments):
                raise _err("23505", "creator_job_deal_payments_one_open_per_deal")

    def _check_payment(self, r: dict[str, Any]) -> None:
        b, fee = r["base_amount_cents"], _fee(r["base_amount_cents"])
        ok = (b > 0 and r["payer_fee_cents"] == fee and r["recipient_fee_cents"] == fee
              and r["total_amount_cents"] == b + fee and r["recipient_amount_cents"] == b - fee
              and r["application_fee_cents"] == 2 * fee and 50 <= r["total_amount_cents"] <= 99_999_999
              and r.get("currency", "usd") == "usd"
              and re.fullmatch(r"acct_[A-Za-z0-9]+", r["recipient_stripe_account_id"])
              and r["status"] in ("pending", "succeeded", "failed", "expired")
              and (r["status"] == "succeeded") == (r.get("succeeded_at") is not None)
              and (r["status"] != "succeeded" or r.get("stripe_checkout_session_id")))
        if not ok:
            raise _err("23514", "creator_job_deal_payments check")

    def check_update(self, name: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        if name == "creator_job_deal_payments":
            terms = ("deal_id", "payer_user_id", "recipient_user_id", "base_amount_cents",
                     "total_amount_cents", "application_fee_cents", "recipient_stripe_account_id")
            if any(old[k] != new[k] for k in terms):
                raise _err("23514", "terms are immutable")
            if old["status"] != "pending" and new["status"] != old["status"]:
                raise _err("23514", f"status {old['status']} is final")
            if old.get("stripe_checkout_session_id") and new.get("stripe_checkout_session_id") != old[
                    "stripe_checkout_session_id"]:
                raise _err("23514", "checkout session is fixed")
            self._check_payment(new)
        if name == "creator_job_deals" and (
                new["status"] not in ("active", "funded")
                or (new["status"] == "funded") != (new.get("funded_at") is not None)):
            raise _err("23514", "creator_job_deals status")
        if name == "creator_job_offers" and new.get("status") not in ("sent", "accepted", "declined"):
            raise _err("23514", "creator_job_offers_status_check")

    def after_update(self, name: str, old: dict[str, Any], new: dict[str, Any]) -> None:
        if (name == "creator_job_offers" and new["status"] == "accepted" and old["status"] != "accepted"
                and not any(d["offer_id"] == new["id"] for d in self.deals)):   # 0051 trigger
            row = {k: new[k] for k in ("application_id", "listing_id", "poster_user_id",
                                       "applicant_user_id", "amount_cents", "currency",
                                       "deliverables", "due_date")}
            row.update(id=str(uuid4()), offer_id=new["id"], created_at=self.now())
            self.check_insert("creator_job_deals", row)
            self.deals.append(row)
        if name == "creator_job_deal_payments" and new["status"] == "succeeded" and old["status"] != "succeeded":
            for d in self.deals:                                           # 0052 trigger
                if d["id"] == new["deal_id"] and d["status"] == "active":
                    d.update(status="funded", funded_at=new["succeeded_at"])


class _FakeStripe:
    """checkout.sessions create/retrieve (with Stripe's idempotency) and payout
    readiness: the Accounts v2 recipient read (raw_request), with v1
    accounts.retrieve for accounts created through Accounts v1."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.by_key: dict[str, str] = {}
        self.creates: list[tuple[dict[str, Any], dict[str, Any]]] = []
        self.retrieves: list[str] = []
        self.ready: set[str] = set()
        self.v1_created: set[str] = set()
        self.fail_create: Exception | None = None
        self.v1 = SimpleNamespace(
            checkout=SimpleNamespace(sessions=SimpleNamespace(create=self._create, retrieve=self._retrieve)),
            accounts=SimpleNamespace(retrieve=self._account),
        )

    def _create(self, params: dict[str, Any], options: dict[str, Any]) -> dict[str, Any]:
        self.creates.append((params, options))
        if self.fail_create is not None:
            raise self.fail_create
        key = options["idempotency_key"]
        if key in self.by_key:
            return dict(self.sessions[self.by_key[key]])
        sid = f"cs_test_{len(self.sessions) + 1}"
        total = sum(li["price_data"]["unit_amount"] * li["quantity"] for li in params["line_items"])
        self.sessions[sid] = {"id": sid, "object": "checkout.session", "status": "open",
                              "url": f"https://checkout.stripe.com/c/pay/{sid}", "amount_total": total,
                              "currency": "usd", "mode": params["mode"], "payment_status": "unpaid",
                              "metadata": dict(params["metadata"]), "_params": params}
        self.by_key[key] = sid
        return dict(self.sessions[sid])

    def _retrieve(self, sid: str) -> dict[str, Any]:
        self.retrieves.append(sid)
        return dict(self.sessions[sid])

    def raw_request(self, method: str, path: str, **params: Any) -> SimpleNamespace:
        assert method == "get" and path.startswith("/v2/core/accounts/"), (method, path)
        assert params == {"include[0]": "configuration.recipient", "stripe_version": "2026-09-30.endive"}
        acct = path.rsplit("/", 1)[1]
        if acct in self.v1_created:
            import stripe

            raise stripe.InvalidRequestError("V1 Account ID cannot be used in V2 Account APIs.", None,
                                             code="v1_account_instead_of_v2_account", http_status=400)
        status = "active" if acct in self.ready else "restricted"
        return SimpleNamespace(data={"id": acct, "object": "v2.core.account", "configuration": {"recipient": {
            "applied": True, "capabilities": {"stripe_balance": {
                "stripe_transfers": {"status": status, "status_details": []},
                "payouts": {"status": status, "status_details": []}}}}}})

    def _account(self, acct: str) -> dict[str, Any]:
        assert acct in self.v1_created, "v2 accounts are read through /v2/core/accounts"
        if acct in self.ready:
            return {"id": acct, "payouts_enabled": True, "capabilities": {"transfers": "active"}}
        return {"id": acct, "payouts_enabled": False, "capabilities": {"transfers": "inactive"}}


class _World:
    def __init__(self, db: _FakeDB, stripe: _FakeStripe) -> None:
        self.db, self.stripe = db, stripe

    def creator(self, name: str = "Sofia Ramirez", *, payouts: str | None = "ready") -> str:
        uid = str(uuid4())
        self.db.tables["creator_profiles"].append({
            "user_id": uid, "full_name": name, "instagram_handle": "sofia", "profile_photo_url": None,
            "onboarding_completed_at": "2026-01-01T00:00:00Z", "niches": [], "content_formats": [],
            "hard_limits": [], "bio": None})
        if payouts is not None:
            acct = f"acct_{uid.replace('-', '')[:16]}"
            self.db.tables["creator_payout_accounts"].append(
                {"creator_user_id": uid, "stripe_account_id": acct})
            if payouts == "ready":
                self.stripe.ready.add(acct)
        return uid

    def brand(self, name: str = "Olipop") -> str:
        uid = str(uuid4())
        self.db.tables["brand_profiles"].append({
            "user_id": uid, "company_name": name, "logo_url": None,
            "onboarding_completed_at": "2026-01-01T00:00:00Z", "niche_preferences": []})
        return uid

    def account_of(self, uid: str) -> str:
        return next(r["stripe_account_id"] for r in self.db.tables["creator_payout_accounts"]
                    if r["creator_user_id"] == uid)


@pytest.fixture()
def db() -> _FakeDB:
    return _FakeDB()


@pytest.fixture()
def stripe_fake() -> _FakeStripe:
    return _FakeStripe()


@pytest.fixture(autouse=True)
def no_money_movement_or_human_dms(monkeypatch: pytest.MonkeyPatch) -> None:
    """Human DMs, LLM calls and deal memory must never run in Step 7A flows."""
    guarded = {"app.services.dms": {"send_message", "get_or_create_thread", "send"},
               "app.services.babyg_deals": None, "app.services.deal_manager": None,
               "app.integrations.anthropic_client": None}
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
                raise AssertionError(f"Step 7A touched {_m}.{_a}")

            monkeypatch.setattr(mod, attr, _boom, raising=False)


@pytest.fixture()
def world(monkeypatch: pytest.MonkeyPatch, db: _FakeDB, stripe_fake: _FakeStripe) -> _World:
    w = _World(db, stripe_fake)
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: db)
    monkeypatch.setattr(creator_payouts, "get_stripe_client", lambda: stripe_fake)
    monkeypatch.setattr(creator_payouts, "get_settings", lambda: SimpleNamespace(
        stripe_secret_key="sk_test_step7a", public_app_url="https://www.babyg.ai",
        app_url="http://localhost:8000", is_production=True))
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", WEBHOOK_SECRET)
    get_settings.cache_clear()
    monkeypatch.setattr(dms, "list_threads_for_user", lambda uid: [])
    monkeypatch.setattr(dms, "unread_counts_by_thread", lambda uid, ids: {})
    monkeypatch.setattr(dms, "unread_count_for_user", lambda uid: 0)
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
    # the Manager chat page's other sources
    monkeypatch.setattr(bot, "list_messages", lambda uid: [])
    monkeypatch.setattr(bot_nudges, "generate_pending", lambda uid: None)
    monkeypatch.setattr(babyg_awareness, "snapshot", lambda uid: {})
    monkeypatch.setattr(manager_activity, "list_recent_activity", lambda uid: [])
    monkeypatch.setattr(manager_activity, "has_new_since", lambda uid: False)
    yield w
    get_settings.cache_clear()


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _sign_in(client: TestClient, uid: str, role: str = "creator") -> str:
    client.cookies.clear()
    resp = Response()
    write_session(resp, {"user_id": uid, "role": role})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])
    return uid


def _deal(client: TestClient, world: _World, *, poster: str = "brand", amount_cents: int = 100000,
          recipient_payouts: str | None = "ready", brand_name: str = "Olipop",
          creator_name: str = "Sofia Ramirez") -> SimpleNamespace:
    """Listing -> application -> offer -> accepted through the REAL 6B route
    (which is what creates the deal and records the Manager events)."""
    payer = world.brand(brand_name) if poster == "brand" else world.creator("Poster Pat", payouts=None)
    lst = {"id": str(uuid4()), "poster_user_id": payer, "poster_role": poster, "title": "Fall Campaign",
           "description": "Reels.", "listing_type": "brand_deal", "compensation_text": "$1k",
           "budget_min": None, "budget_max": None, "target_niches": [], "deadline": None,
           "is_active": True, "is_taken_down": False, "discovery_eligible": True, "expires_at": None,
           "location_city": None, "location_region": None, "created_at": "2026-01-01T00:00:00Z"}
    world.db.tables["creator_job_listings"].append(lst)
    recipient = world.creator(creator_name, payouts=recipient_payouts)
    app_id, offer_id = str(uuid4()), str(uuid4())
    world.db.tables["creator_job_applications"].append(
        {"id": app_id, "listing_id": lst["id"], "applicant_user_id": recipient, "message": "Pick me.",
         "status": "submitted", "created_at": "2026-03-04T10:30:00Z"})
    world.db.tables["creator_job_offers"].append({
        "id": offer_id, "application_id": app_id, "listing_id": lst["id"], "poster_user_id": payer,
        "applicant_user_id": recipient, "amount_cents": amount_cents, "currency": "USD",
        "deliverables": "2 TikToks", "due_date": "2026-10-30", "note": None, "status": "sent",
        "viewed_at": None, "responded_at": None, "created_at": world.db.now()})
    _sign_in(client, recipient)
    assert client.post(f"/creator/dm/offers/{offer_id}/accept").status_code == 303
    [deal] = [d for d in world.db.deals if d["offer_id"] == offer_id]
    tree = "brand" if poster == "brand" else "creator"
    return SimpleNamespace(payer=payer, recipient=recipient, deal=deal, id=deal["id"], offer_id=offer_id,
                           payer_role=tree, payer_path=f"/{tree}/dm/deals/{deal['id']}",
                           recipient_path=f"/creator/dm/deals/{deal['id']}")


def _as_payer(client: TestClient, s: SimpleNamespace) -> None:
    _sign_in(client, s.payer, s.payer_role)


def _pay(client: TestClient, s: SimpleNamespace, **data: Any) -> Any:
    _as_payer(client, s)
    return client.post(f"{s.payer_path}/pay", data=data or None)


def _article(html: str) -> str:
    return html.split("<article", 1)[1].split("</article>", 1)[0]


def _signed(body: bytes) -> dict[str, str]:
    ts = int(time.time())
    mac = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.{body.decode()}".encode(), hashlib.sha256).hexdigest()
    return {"Stripe-Signature": f"t={ts},v1={mac}", "Content-Type": "application/json"}


def _event(event_type: str, session: dict[str, Any], *, event_id: str | None = None,
           livemode: bool = False) -> bytes:
    obj = {k: v for k, v in session.items() if not k.startswith("_")}
    return json.dumps({"id": event_id or f"evt_{uuid4().hex[:12]}", "object": "event", "type": event_type,
                       "livemode": livemode, "created": int(time.time()),
                       "data": {"object": obj}}).encode()


def _post_event(client: TestClient, body: bytes) -> Any:
    return client.post("/webhooks/stripe", content=body, headers=_signed(body))


def _paid(world: _World, session_id: str, **over: Any) -> dict[str, Any]:
    s = dict(world.stripe.sessions[session_id])
    s.update(status="complete", payment_status="paid", payment_intent="pi_test_123", **over)
    world.stripe.sessions[session_id].update(status="complete", payment_status="paid")
    return s


# ==================================================================== ECONOMICS


def test_locked_economics_for_a_1000_dollar_deal():
    b = deal_payments.breakdown(100_000)
    assert (b.base_cents, b.payer_fee_cents, b.total_cents) == (100_000, 10_000, 110_000)
    assert (b.recipient_fee_cents, b.recipient_cents) == (10_000, 90_000)
    assert b.platform_fee_cents == 20_000            # babyg gross, before Stripe costs
    assert b.total_cents - b.recipient_cents == b.platform_fee_cents
    for v in (b.base_cents, b.payer_fee_cents, b.total_cents, b.recipient_cents, b.platform_fee_cents):
        assert type(v) is int


@pytest.mark.parametrize(("base", "fee"), [(1, 0), (4, 0), (5, 1), (15, 2), (25050, 2505),
                                           (33333, 3333), (99995, 10000), (1_000_000_000, 100_000_000)])
def test_fee_is_ten_percent_half_up_in_whole_cents_matching_the_db_check(base, fee):
    b = deal_payments.breakdown(base)
    assert b.payer_fee_cents == b.recipient_fee_cents == fee == _fee(base)
    assert b.total_cents == base + fee and b.recipient_cents == base - fee


@pytest.mark.parametrize("bad", [0, -100, 10.5, "100", True, None])
def test_breakdown_accepts_only_positive_integer_cents(bad):
    with pytest.raises(ValueError):
        deal_payments.breakdown(bad)


@pytest.mark.parametrize(("base", "payable"), [(45, True), (44, False), (90_909_090, True),
                                               (90_909_091, False), (1_000_000_000, False)])
def test_card_charge_limits(base, payable):
    assert deal_payments.breakdown(base).payable is payable


# ================================================================ AUTHORIZATION


def test_unauthenticated_pay_is_refused_on_both_trees(client, world):
    s = _deal(client, world)
    client.cookies.clear()
    for url in (f"/creator/dm/deals/{s.id}/pay", f"/brand/dm/deals/{s.id}/pay"):
        assert client.post(url).status_code in (302, 303, 401, 403), url
    assert world.db.payments == [] and world.stripe.creates == []


def test_recipient_cannot_pay_their_own_deal(client, world):
    s = _deal(client, world)
    _sign_in(client, s.recipient)
    assert client.post(f"/creator/dm/deals/{s.id}/pay").status_code == 404
    assert world.db.payments == [] and world.stripe.creates == []


def test_nonparticipants_get_404_and_nothing_is_created(client, world):
    s = _deal(client, world)
    _sign_in(client, world.creator("Stranger"))
    assert client.post(f"/creator/dm/deals/{s.id}/pay").status_code == 404
    _sign_in(client, world.brand("Other Brand"), "brand")
    assert client.post(f"/brand/dm/deals/{s.id}/pay").status_code == 404
    assert world.db.payments == [] and world.stripe.creates == []


def test_id_tampering_cannot_pay_another_users_deal(client, world):
    a = _deal(client, world, brand_name="Brand A")
    b = _deal(client, world, brand_name="Brand B")
    _as_payer(client, a)
    assert client.post(f"/brand/dm/deals/{b.id}/pay").status_code == 404
    for bad in ("not-a-uuid", "1' or '1'='1", str(uuid4())):
        assert client.post(f"/brand/dm/deals/{bad}/pay").status_code == 404
    assert world.db.payments == [] and world.stripe.creates == []


def test_wrong_role_tree_is_refused(client, world):
    s = _deal(client, world)
    _sign_in(client, s.payer, "brand")
    assert client.post(f"/creator/dm/deals/{s.id}/pay").status_code == 403
    _sign_in(client, s.recipient)
    assert client.post(f"/brand/dm/deals/{s.id}/pay").status_code == 403


def test_csrf_guards_pay(world, monkeypatch):
    c = TestClient(app, follow_redirects=False)
    s = _deal(c, world)
    monkeypatch.setattr(_csrf_module.CSRFMiddleware, "__call__", _REAL_CSRF_CALL)
    _as_payer(c, s)
    assert c.post(f"{s.payer_path}/pay", headers={"Origin": "http://testserver"}).status_code == 403
    assert world.db.payments == [] and world.stripe.creates == []


# ===================================================================== CHECKOUT


def test_payer_pay_opens_a_destination_charge_checkout_built_from_the_deal(client, world):
    s = _deal(client, world, amount_cents=100000)
    r = _pay(client, s)
    assert r.status_code == 303 and r.headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_1"
    [payment] = world.db.payments
    assert payment["status"] == "pending" and payment["stripe_checkout_session_id"] == "cs_test_1"
    assert (payment["base_amount_cents"], payment["total_amount_cents"], payment["recipient_amount_cents"],
            payment["application_fee_cents"]) == (100000, 110000, 90000, 20000)
    assert payment["payer_user_id"] == s.payer and payment["recipient_user_id"] == s.recipient
    [(params, options)] = world.stripe.creates
    acct = world.account_of(s.recipient)
    meta = {"babyg_deal_id": s.id, "babyg_payment_id": payment["id"]}
    assert params == {
        "mode": "payment",
        "payment_method_types": ["card"],
        "line_items": [
            {"quantity": 1, "price_data": {"currency": "usd", "unit_amount": 100000,
                                           "product_data": {"name": "Deal value"}}},
            {"quantity": 1, "price_data": {"currency": "usd", "unit_amount": 10000,
                                           "product_data": {"name": "babyg fee"}}},
        ],
        "client_reference_id": s.id,
        "metadata": meta,
        "payment_intent_data": {"application_fee_amount": 20000, "transfer_data": {"destination": acct},
                                "transfer_group": f"deal_{s.id}", "metadata": meta},
        "success_url": f"https://www.babyg.ai{s.payer_path}?payment=processing",
        "cancel_url": f"https://www.babyg.ai{s.payer_path}",
    }
    assert options == {"idempotency_key": f"babyg-deal-checkout-{payment['id']}"}
    # the redirect is NOT payment: nothing is funded yet
    assert world.db.deals[0]["status"] == "active" and world.db.deals[0]["funded_at"] is None


def test_creator_poster_pays_through_the_creator_tree(client, world):
    s = _deal(client, world, poster="creator", amount_cents=25050)
    r = _pay(client, s)
    assert r.headers["location"].startswith("https://checkout.stripe.com/")
    params = world.stripe.creates[0][0]
    assert params["success_url"] == f"https://www.babyg.ai/creator/dm/deals/{s.id}?payment=processing"
    assert [li["price_data"]["unit_amount"] for li in params["line_items"]] == [25050, 2505]
    assert params["payment_intent_data"]["application_fee_amount"] == 5010


def test_tampered_form_fields_are_ignored(client, world):
    s = _deal(client, world)
    attacker = world.creator("Attacker")
    _pay(client, s, amount="1", amount_cents="1", fee="0", total="1", recipient=attacker,
         payer=s.recipient, stripe_account="acct_attacker", destination="acct_attacker",
         status="succeeded", deal_id=str(uuid4()))
    [(params, _)] = world.stripe.creates
    assert params["payment_intent_data"]["transfer_data"]["destination"] == world.account_of(s.recipient)
    assert sum(li["price_data"]["unit_amount"] for li in params["line_items"]) == 110000
    assert world.db.payments[0]["status"] == "pending" and world.db.deals[0]["status"] == "active"


def test_repeat_pay_reuses_the_open_checkout_no_duplicate_payment(client, world):
    s = _deal(client, world)
    first = _pay(client, s).headers["location"]
    for _ in range(3):
        assert _pay(client, s).headers["location"] == first
    assert len(world.db.payments) == 1 and len(world.stripe.creates) == 1
    assert world.stripe.retrieves == ["cs_test_1"] * 3


def test_concurrent_attempt_without_a_session_shares_the_same_idempotency_key(client, world):
    s = _deal(client, world)
    b = deal_payments.breakdown(100000)
    world.db.tables["creator_job_deal_payments"].append({   # another request inserted, mid-flight
        "id": str(uuid4()), "deal_id": s.id, "payer_user_id": s.payer, "recipient_user_id": s.recipient,
        "base_amount_cents": 100000, "payer_fee_cents": b.payer_fee_cents,
        "recipient_fee_cents": b.recipient_fee_cents, "total_amount_cents": b.total_cents,
        "recipient_amount_cents": b.recipient_cents, "application_fee_cents": b.platform_fee_cents,
        "currency": "usd", "recipient_stripe_account_id": world.account_of(s.recipient),
        "status": "pending", "stripe_checkout_session_id": None, "created_at": world.db.now()})
    pid = world.db.payments[0]["id"]
    world.stripe.by_key[f"babyg-deal-checkout-{pid}"] = "cs_test_9"   # its session already exists
    world.stripe.sessions["cs_test_9"] = {"id": "cs_test_9", "status": "open", "amount_total": 110000,
                                          "url": "https://checkout.stripe.com/c/pay/cs_test_9"}
    assert _pay(client, s).headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_9"
    assert len(world.db.payments) == 1 and world.db.payments[0]["stripe_checkout_session_id"] == "cs_test_9"


def test_one_open_payment_per_deal_is_enforced_by_the_database(world, client):
    s = _deal(client, world)
    _pay(client, s)
    row = {k: v for k, v in world.db.payments[0].items() if k not in ("id", "created_at", "updated_at")}
    row.update(status="pending", stripe_checkout_session_id=None)
    with pytest.raises(PostgrestAPIError):
        world.db.table("creator_job_deal_payments").insert(row).execute()
    tampered = dict(row, base_amount_cents=100, payer_fee_cents=10, recipient_fee_cents=10,
                    total_amount_cents=110, recipient_amount_cents=90, application_fee_cents=20)
    with pytest.raises(PostgrestAPIError):
        world.db.table("creator_job_deal_payments").insert(tampered).execute()


def test_recipient_without_payouts_blocks_checkout_and_both_see_why(client, world):
    s = _deal(client, world, recipient_payouts=None, creator_name="Sofia Ramirez")
    r = _pay(client, s)
    assert r.headers["location"] == f"{s.payer_path}?payment=recipient"
    assert world.db.payments == [] and world.stripe.creates == []
    art = _article(client.get(r.headers["location"]).text)
    assert "Sofia Ramirez needs to finish payout setup before you can pay." in art
    assert "<form" not in art and "Pay $" not in art
    _sign_in(client, s.recipient)
    art = _article(client.get(s.recipient_path).text)
    assert "Set up payouts so Olipop can pay." in art
    assert '<a href="/creator/profile/settings#payouts" class="btn btn-ghost btn-sm opportunity-deal-setup">' \
           'set up payouts</a>' in art


def test_incomplete_recipient_account_also_blocks(client, world):
    s = _deal(client, world, recipient_payouts="incomplete")
    assert _pay(client, s).headers["location"].endswith("?payment=recipient")
    assert world.stripe.creates == []


def test_stripe_failure_retires_the_attempt_so_the_next_pay_is_fresh(client, world):
    import stripe

    s = _deal(client, world)
    world.stripe.fail_create = stripe.InvalidRequestError("connected account cannot receive", None)
    r = _pay(client, s)
    assert r.headers["location"] == f"{s.payer_path}?payment=error"
    assert [p["status"] for p in world.db.payments] == ["failed"]
    art = _article(client.get(r.headers["location"]).text)
    assert "couldn&#39;t open payment. try again." in art and "Pay $1,100" in art
    world.stripe.fail_create = None
    assert _pay(client, s).headers["location"].startswith("https://checkout.stripe.com/")
    keys = [o["idempotency_key"] for _, o in world.stripe.creates]
    assert len(set(keys)) == 2   # a new attempt, a new key (no 24h replay of the failure)
    assert sorted(p["status"] for p in world.db.payments) == ["failed", "pending"]


def test_expired_checkout_is_retired_and_a_new_one_opens(client, world):
    s = _deal(client, world)
    _pay(client, s)
    world.stripe.sessions["cs_test_1"]["status"] = "expired"
    assert _pay(client, s).headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_2"
    assert [p["status"] for p in world.db.payments] == ["expired", "pending"]


def test_completed_checkout_awaiting_webhook_never_opens_a_second(client, world):
    s = _deal(client, world)
    _pay(client, s)
    world.stripe.sessions["cs_test_1"]["status"] = "complete"
    assert _pay(client, s).headers["location"] == f"{s.payer_path}?payment=processing"
    assert len(world.stripe.creates) == 1 and len(world.db.payments) == 1


def test_browser_success_redirect_is_not_proof_of_payment(client, world):
    s = _deal(client, world)
    _pay(client, s)
    _as_payer(client, s)
    art = _article(client.get(f"{s.payer_path}?payment=processing").text)
    assert "Stripe is confirming your payment. Refresh in a moment." in art
    assert "<form" not in art and ">active<" in art and "Funded" not in art
    assert world.db.deals[0]["status"] == "active" and world.db.payments[0]["status"] == "pending"


def test_out_of_range_deal_has_no_pay_button_and_no_checkout(client, world):
    s = _deal(client, world, amount_cents=1_000_000_000)
    _as_payer(client, s)
    art = _article(client.get(s.payer_path).text)
    assert "This total is outside the card payment limit." in art and "<form" not in art
    assert _pay(client, s).headers["location"].endswith("?payment=limit")
    assert world.stripe.creates == []


def test_live_keys_are_refused_sandbox_only(client, world, monkeypatch):
    s = _deal(client, world)
    monkeypatch.setattr(creator_payouts, "get_settings", lambda: SimpleNamespace(
        stripe_secret_key="sk_live_never", public_app_url="https://www.babyg.ai", app_url="",
        is_production=True))
    assert _pay(client, s).headers["location"].endswith("?payment=error")
    assert world.stripe.creates == [] and world.db.payments == []


def test_funded_deal_never_opens_checkout_again(client, world):
    s = _deal(client, world)
    _pay(client, s)
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1")))
    assert _pay(client, s).headers["location"] == s.payer_path
    assert len(world.stripe.creates) == 1 and len(world.db.payments) == 1


# ====================================================================== WEBHOOK


def test_webhook_confirmed_payment_funds_the_deal_once(client, world):
    s = _deal(client, world)
    _pay(client, s)
    body = _event("checkout.session.completed", _paid(world, "cs_test_1"), event_id="evt_paid_1")
    r = _post_event(client, body)
    assert r.status_code == 200 and r.json() == {"received": True, "event_id": "evt_paid_1"}
    [p] = world.db.payments
    assert p["status"] == "succeeded" and p["stripe_payment_intent_id"] == "pi_test_123"
    assert p["last_stripe_event_id"] == "evt_paid_1" and p["succeeded_at"]
    d = world.db.deals[0]
    assert d["status"] == "funded" and d["funded_at"] == p["succeeded_at"]


def test_duplicate_and_replayed_webhooks_change_nothing_twice(client, world):
    s = _deal(client, world)
    _pay(client, s)
    paid = _paid(world, "cs_test_1")
    body = _event("checkout.session.completed", paid, event_id="evt_dup")
    for _ in range(3):
        assert _post_event(client, body).status_code == 200
    assert _post_event(client, _event("checkout.session.async_payment_succeeded", paid)).status_code == 200
    snapshot = (json.dumps(world.db.payments, sort_keys=True), json.dumps(world.db.deals, sort_keys=True))
    funded_notes = [n for n in world.db.notes if n["source_event_id"] == f"deal.funded:{s.id}"]
    assert len(funded_notes) == 2   # one per party, ever
    _post_event(client, body)
    assert (json.dumps(world.db.payments, sort_keys=True), json.dumps(world.db.deals, sort_keys=True)) == snapshot
    assert len([n for n in world.db.notes if n["source_event_id"] == f"deal.funded:{s.id}"]) == 2
    updates = [q for q in world.db.queries if q["table"] == "creator_job_deal_payments" and q["op"] == "update"
               and (q["body"] or {}).get("status") == "succeeded"]
    assert all(("eq", "status", "pending") in q["filters"] for q in updates)


def test_invalid_signature_changes_nothing(client, world):
    s = _deal(client, world)
    _pay(client, s)
    body = _event("checkout.session.completed", _paid(world, "cs_test_1"))
    bad = {"Stripe-Signature": f"t={int(time.time())},v1={'0' * 64}", "Content-Type": "application/json"}
    assert client.post("/webhooks/stripe", content=body, headers=bad).status_code == 400
    assert client.post("/webhooks/stripe", content=body).status_code == 400
    assert world.db.payments[0]["status"] == "pending" and world.db.deals[0]["status"] == "active"


@pytest.mark.parametrize("over", [{"amount_total": 100}, {"amount_total": 100000}, {"currency": "eur"},
                                  {"mode": "subscription"}])
def test_amount_or_currency_mismatch_never_funds(client, world, over):
    s = _deal(client, world)
    _pay(client, s)
    assert _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1", **over))
                       ).status_code == 200
    assert world.db.payments[0]["status"] == "pending" and world.db.deals[0]["status"] == "active"


def test_forged_or_foreign_metadata_never_funds(client, world):
    a = _deal(client, world, brand_name="Brand A")
    b = _deal(client, world, brand_name="Brand B")
    _pay(client, a)
    _pay(client, b)
    pa = next(p for p in world.db.payments if p["deal_id"] == a.id)
    paid_a = _paid(world, pa["stripe_checkout_session_id"])
    cases = [
        dict(paid_a, metadata={"babyg_deal_id": b.id, "babyg_payment_id": pa["id"]}),     # other deal
        dict(paid_a, metadata={"babyg_deal_id": a.id, "babyg_payment_id": str(uuid4())}),  # unknown payment
        dict(paid_a, id="cs_test_999"),                                                    # other session
        dict(paid_a, metadata={}),                                                         # not babyg
        dict(paid_a, metadata={"babyg_deal_id": "x", "babyg_payment_id": "1' or 1=1"}),
    ]
    for case in cases:
        assert _post_event(client, _event("checkout.session.completed", case)).status_code == 200
    assert all(p["status"] == "pending" for p in world.db.payments)
    assert all(d["status"] == "active" for d in world.db.deals)


def test_livemode_events_are_ignored(client, world):
    s = _deal(client, world)
    _pay(client, s)
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1"), livemode=True))
    assert world.db.deals[0]["status"] == "active"


def test_async_unpaid_then_failed_never_funds_and_allows_a_new_attempt(client, world):
    s = _deal(client, world)
    _pay(client, s)
    unpaid = dict(world.stripe.sessions["cs_test_1"], status="complete", payment_status="unpaid")
    _post_event(client, _event("checkout.session.completed", unpaid))
    assert world.db.payments[0]["status"] == "pending" and world.db.deals[0]["status"] == "active"
    _post_event(client, _event("checkout.session.async_payment_failed", unpaid))
    assert world.db.payments[0]["status"] == "failed" and world.db.deals[0]["status"] == "active"
    # a late "paid" for a failed attempt cannot resurrect it
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1")))
    assert world.db.payments[0]["status"] == "failed" and world.db.deals[0]["status"] == "active"
    assert _pay(client, s).headers["location"] == "https://checkout.stripe.com/c/pay/cs_test_2"


def test_expired_event_retires_the_attempt(client, world):
    s = _deal(client, world)
    _pay(client, s)
    expired = dict(world.stripe.sessions["cs_test_1"], status="expired")
    _post_event(client, _event("checkout.session.expired", expired))
    _post_event(client, _event("checkout.session.expired", expired))
    assert world.db.payments[0]["status"] == "expired" and world.db.deals[0]["status"] == "active"


def test_transient_db_failure_asks_stripe_to_retry_then_funds_once(client, world):
    s = _deal(client, world)
    _pay(client, s)
    body = _event("checkout.session.completed", _paid(world, "cs_test_1"), event_id="evt_retry")
    world.db.fail_ops.add(("update", "creator_job_deal_payments"))
    assert _post_event(client, body).status_code == 500
    assert world.db.deals[0]["status"] == "active"
    world.db.fail_ops.clear()
    assert _post_event(client, body).status_code == 200
    assert world.db.deals[0]["status"] == "funded"
    assert len([n for n in world.db.notes if n["source_event_id"].startswith("deal.funded")]) == 2


def test_other_stripe_events_never_touch_the_database(client, world):
    _deal(client, world)
    before = len(world.db.queries)
    body = json.dumps({"id": "evt_x", "object": "event", "type": "charge.succeeded", "livemode": False,
                       "data": {"object": {"id": "ch_1"}}}).encode()
    assert _post_event(client, body).status_code == 200
    foreign = _event("checkout.session.completed", {"id": "cs_other", "metadata": {"order": "1"}})
    assert _post_event(client, foreign).status_code == 200
    assert len(world.db.queries) == before


# ====================================================================== UI


def test_payer_sees_payment_required_with_the_exact_breakdown_and_pay(client, world):
    s = _deal(client, world, amount_cents=100000)
    _as_payer(client, s)
    art = _article(client.get(s.payer_path).text)
    sec = art.split('data-deal-payment="payer"', 1)[1].split("</section>", 1)[0]
    assert "<strong>Payment required</strong>" in sec
    assert re.findall(r"<dt>([^<]+)</dt><dd>([^<]+)</dd>", sec) == [
        ("Deal value", "$1,000"), ("babyg fee", "$100"), ("Total", "$1,100")]
    assert f'<form method="post" action="/brand/dm/deals/{s.id}/pay" class="opportunity-deal-pay">' in sec
    assert 'name="csrf_token"' in sec or "csrf" in sec
    assert '<button type="submit" class="btn btn-lime">Pay $1,100</button>' in sec
    assert "<p>Deal active.</p><p>Payment is the next step.</p>" in art
    for banned in ("escrow", "held securely", "release", "refund", "transaction", "cs_test", "acct_"):
        assert banned not in art.lower(), banned


def test_recipient_sees_awaiting_payment_and_their_expected_amount_only(client, world):
    s = _deal(client, world, amount_cents=100000)
    _sign_in(client, s.recipient)
    art = _article(client.get(s.recipient_path).text)
    sec = art.split('data-deal-payment="recipient"', 1)[1].split("</section>", 1)[0]
    assert "<strong>Awaiting payment</strong>" in sec
    assert re.findall(r"<dt>([^<]+)</dt><dd>([^<]+)</dd>", sec) == [
        ("Deal value", "$1,000"), ("babyg fee", "$100"), ("Expected amount", "$900")]
    assert "$1,100" not in art and "Total" not in sec
    assert "<form" not in art and "<button" not in art and "/pay" not in art


@pytest.mark.parametrize("viewer", ["payer", "recipient"])
def test_funded_ui_for_both_parties(client, world, viewer):
    s = _deal(client, world)
    _pay(client, s)
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1")))
    if viewer == "payer":
        _as_payer(client, s)
        html = client.get(s.payer_path).text
    else:
        _sign_in(client, s.recipient)
        html = client.get(s.recipient_path).text
    art = _article(html)
    assert '<span class="badge badge-muted">funded</span>' in art
    assert '<p class="opportunity-deal-payment-state"><strong>Funded</strong></p>' in art
    assert "<h2>babyg</h2>\n    <p>Payment confirmed. Work can begin.</p>" in art
    assert "Payment is the next step." not in art
    assert "<form" not in art and "<button" not in art
    last = ("Total", "$1,100") if viewer == "payer" else ("Expected amount", "$900")
    assert re.findall(r"<dt>([^<]+)</dt><dd>([^<]+)</dd>", art)[-1] == last
    for banned in ("pi_test", "cs_test", "evt_", "acct_", "transaction", "held securely", "escrow"):
        assert banned not in art


def test_deals_list_shows_funded_for_both_parties(client, world):
    s = _deal(client, world)
    _pay(client, s)
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1")))
    _sign_in(client, s.recipient)
    assert "<strong>$1,000</strong> · funded" in client.get("/creator/dm?view=deals").text
    _as_payer(client, s)
    assert "<strong>$1,000</strong> · funded" in client.get("/brand/dm?view=deals").text


# ====================================================================== MANAGER


def _manager_rows(db: _FakeDB, uid: str) -> list[dict[str, Any]]:
    return [n for n in db.notes if n["user_id"] == uid]


def test_accept_records_grounded_awaiting_payment_events_for_both_parties(client, world):
    s = _deal(client, world, brand_name="Olipop", creator_name="Sofia Ramirez")
    [payer_row] = _manager_rows(world.db, s.payer)
    [recipient_row] = _manager_rows(world.db, s.recipient)
    assert payer_row["title"] == "Your deal with Sofia Ramirez is ready for payment."
    assert payer_row["link_path"] == f"/brand/dm/deals/{s.id}"
    assert recipient_row["title"] == "Your deal with Olipop is awaiting payment."
    assert recipient_row["link_path"] == f"/creator/dm/deals/{s.id}"
    for row in (payer_row, recipient_row):
        assert row["kind"] == "manager_alert" and row["source_provider"] == "babyg"
        assert row["source_event_id"] == f"deal.awaiting_payment:{s.id}"
        assert row["underlying_type"] == "creator_job_deal" and row["underlying_id"] == s.id
        assert row["metadata"] == {"event": "deal.awaiting_payment", "matter_type": "deal",
                                   "actions": ["view_deal"]}


def test_repeat_accepts_and_revisits_never_duplicate_manager_events(client, world):
    s = _deal(client, world)
    _sign_in(client, s.recipient)
    for _ in range(3):
        client.post(f"/creator/dm/offers/{s.offer_id}/accept")
        client.get(s.recipient_path)
    assert deal_events.record_awaiting_payment(s.id) == 0   # already recorded
    assert len(world.db.notes) == 2


def test_funding_records_payment_confirmed_for_both(client, world):
    s = _deal(client, world, poster="creator", creator_name="Sofia Ramirez")
    _pay(client, s)
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1")))
    funded = {n["user_id"]: n for n in world.db.notes if n["source_event_id"] == f"deal.funded:{s.id}"}
    assert funded[s.payer]["title"] == ("Payment for your deal with Sofia Ramirez is confirmed. "
                                        "The deal is funded.")
    assert funded[s.payer]["link_path"] == f"/creator/dm/deals/{s.id}"   # creator poster -> creator tree
    assert funded[s.recipient]["title"] == ("Payment for your deal with Poster Pat is confirmed. "
                                            "The deal is funded.")


def test_creator_manager_page_shows_the_latest_update_per_deal(client, world):
    s = _deal(client, world, brand_name="Olipop")
    _sign_in(client, s.recipient)
    html = client.get("/creator/bot").text
    sec = html.split("data-manager-updates>", 1)[1].split("</section>", 1)[0]
    assert '<p class="manager-update-text">Your deal with Olipop is awaiting payment.</p>' in sec
    assert f'<a href="/creator/dm/deals/{s.id}" class="manager-update-action">View deal</a>' in sec
    _pay(client, s)
    _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1")))
    _sign_in(client, s.recipient)
    sec = client.get("/creator/bot").text.split("data-manager-updates>", 1)[1].split("</section>", 1)[0]
    assert sec.count("data-manager-update>") == 1   # funded supersedes awaiting payment
    assert "Payment for your deal with Olipop is confirmed. The deal is funded." in sec


def test_brand_manager_surface_on_brand_dms(client, world):
    s = _deal(client, world, creator_name="Sofia Ramirez")
    _as_payer(client, s)
    html = client.get("/brand/dm").text
    sec = html.split("data-manager-updates>", 1)[1].split("</section>", 1)[0]
    assert "Your deal with Sofia Ramirez is ready for payment." in sec
    assert f'<a href="/brand/dm/deals/{s.id}" class="manager-update-action">View deal</a>' in sec
    assert "brand messaging is coming soon" in html   # the placeholder stays


def test_manager_updates_are_private_and_absent_without_events(client, world):
    s = _deal(client, world)
    _sign_in(client, world.creator("Stranger"))
    assert "data-manager-updates" not in client.get("/creator/bot").text
    _sign_in(client, world.brand("Other"), "brand")
    assert "data-manager-updates" not in client.get("/brand/dm").text
    assert s.id not in client.get("/brand/dm").text


def test_manager_list_drops_unknown_actions_and_foreign_links(world, db):
    uid = world.creator("X")
    for i, (path, actions) in enumerate([("https://evil.example/x", ["view_deal"]),
                                         (f"/creator/dm/deals/{uuid4()}", ["accept", "view_deal"])]):
        db.tables["notifications"].append({
            "id": str(uuid4()), "user_id": uid, "kind": "manager_alert", "source_provider": "babyg",
            "source_event_id": f"e{i}", "underlying_type": "creator_job_deal", "underlying_id": str(uuid4()),
            "title": f"t{i}", "link_path": path, "metadata": {"actions": actions}, "archived_at": None,
            "created_at": db.now()})
    rows = deal_events.list_recent(uid, limit=5)
    assert [r["title"] for r in rows] == ["t1"]
    assert rows[0]["actions"] == [{"label": "View deal", "path": rows[0]["actions"][0]["path"]}]


def test_create_once_dedupes_through_the_unique_index(world, db):
    uid = world.creator("X")
    kw = {"user_id": uid, "kind": "manager_alert", "title": "t", "source_provider": "babyg",
          "source_event_id": "deal.funded:1"}
    assert notifications.create_once(**kw) is True
    assert notifications.create_once(**kw) is False
    assert len(db.notes) == 1
    assert notifications.create_once(**{**kw, "kind": "bogus"}) is False


def test_manager_event_failures_never_break_accept_or_funding(client, world, monkeypatch):
    world.db.fail_tables.add("notifications")
    s = _deal(client, world)          # accept still 303s and the deal exists
    _pay(client, s)
    assert _post_event(client, _event("checkout.session.completed", _paid(world, "cs_test_1"))
                       ).status_code == 200
    assert world.db.deals[0]["status"] == "funded"


def test_no_babyg_message_is_injected_into_human_dms():
    src = Path("app/services/deal_events.py").read_text(encoding="utf-8") + Path(
        "app/services/deal_payments.py").read_text(encoding="utf-8")
    for banned in ("dm_messages", "dm_threads", "dms.", "send_message", "instagram_dm", "bot_messages"):
        assert banned not in src, banned


# ======================================================================== SCOPE


def test_step_7a_moves_no_money_beyond_the_checkout_destination_charge():
    import ast

    tree = ast.parse(Path("app/services/deal_payments.py").read_text(encoding="utf-8"))
    for node in ast.walk(tree):   # code only: drop docstrings (comments never reach the AST)
        body = getattr(node, "body", None)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef) and body and isinstance(
                body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(tree)
    for banned in ("transfers.create", "refunds", "payouts.create", "reversals", "disputes",
                   "capture_method", "sk_live", "on_behalf_of"):
        assert banned not in code, banned
    assert "creator_payouts._client()" in code   # the sk_test_-only gate


def test_status_vocabulary_and_babyg_lines_are_fixed():
    assert deal_payments.FUNDED_STATE == ("Payment confirmed. Work can begin.",)
    handled = set(deal_payments.HANDLED_EVENTS)
    assert handled == {"checkout.session.completed", "checkout.session.async_payment_succeeded",
                       "checkout.session.async_payment_failed", "checkout.session.expired"}


MIGRATION = Path("migrations/0052_creator_job_deal_payments.sql")


def test_migration_0052_is_additive_private_idempotent_and_pins_the_economics():
    names = sorted(p.name for p in Path("migrations").glob("*.sql"))
    assert names[-2:] == ["0051_creator_job_deals.sql", MIGRATION.name]
    sql = MIGRATION.read_text(encoding="utf-8")
    code = "\n".join(line.split("--", 1)[0] for line in sql.splitlines()).lower()
    assert code.strip().startswith("begin;") and code.strip().endswith("commit;")
    assert "add column if not exists funded_at timestamptz" in code
    assert code.index("drop constraint if exists creator_job_deals_status_check") < code.index(
        "add constraint creator_job_deals_status_check")
    assert "check (status in ('active', 'funded'))" in code
    assert "check ((status = 'funded') = (funded_at is not null))" in code
    assert "create table if not exists public.creator_job_deal_payments" in code
    assert ("foreign key (deal_id, payer_user_id, recipient_user_id, base_amount_cents)\n"
            "    references public.creator_job_deals (id, poster_user_id, applicant_user_id, amount_cents)") in code
    assert "payer_fee_cents = (base_amount_cents * 1000 + 5000) / 10000" in code
    assert "total_amount_cents between 50 and 99999999" in code
    assert "on public.creator_job_deal_payments (deal_id)\n  where status in ('pending', 'succeeded')" in code
    assert "enable row level security" in code
    assert "revoke all on public.creator_job_deal_payments from anon, authenticated" in code
    assert "grant select, insert, update on public.creator_job_deal_payments to service_role" in code
    assert "grant update (status, funded_at) on public.creator_job_deals to service_role" in code
    assert "when (new.status = 'succeeded' and old.status is distinct from 'succeeded')" in code
    assert "where id = new.deal_id and status = 'active'" in code
    for trig in ("creator_job_deal_payments_guard", "creator_job_deal_payments_fund_deal"):
        assert code.index(f"drop trigger if exists {trig}") < code.index(f"create trigger {trig}")
    for banned in ("drop table", "drop column", "truncate", "delete from", "create policy", "drop policy",
                   " to authenticated", " to anon", "security definer", "creator_job_offers",
                   "insert into"):
        assert banned not in code, banned


def test_v1_created_recipient_accounts_still_fund_through_the_v1_readiness_fallback(client, world):
    s = _deal(client, world)
    world.stripe.v1_created.add(world.account_of(s.recipient))
    assert _pay(client, s).headers["location"].startswith("https://checkout.stripe.com/")
    params = world.stripe.creates[0][0]
    assert params["payment_intent_data"]["transfer_data"]["destination"] == world.account_of(s.recipient)
