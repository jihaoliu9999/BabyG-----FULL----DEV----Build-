"""Sandbox Connect onboarding and creator-only Settings contract.

New connected accounts are created with Accounts v2 (POST /v2/core/accounts,
recipient configuration) because Stripe rejects Accounts v1 creation for this
platform. Accounts created through v1 earlier keep working through v1 reads
and v1 onboarding links.
"""

from __future__ import annotations

import json
import logging
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
import stripe
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import agent_memory, creator_payouts, oauth_connections, profiles

V2_VERSION = "2026-09-30.endive"
EXPECTED_V2_ACCOUNT = {
    "contact_email": "sofia@example.com",
    "identity": {"country": "us"},
    "dashboard": "express",
    "defaults": {"responsibilities": {"fees_collector": "application", "losses_collector": "application"}},
    "configuration": {"recipient": {"capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}}},
}


def _v2_account(status: str = "active", payouts: str | None = "active", applied: bool = True) -> dict:
    balance: dict[str, Any] = {"stripe_transfers": {"status": status, "status_details": []}}
    if payouts is not None:
        balance["payouts"] = {"status": payouts, "status_details": []}
    return {"id": "acct_x", "object": "v2.core.account",
            "configuration": {"recipient": {"applied": applied, "capabilities": {"stripe_balance": balance}}}}


def _v1_account_error() -> stripe.InvalidRequestError:
    return stripe.InvalidRequestError(
        "V1 Account ID cannot be used in V2 Account APIs.", None, code="v1_account_instead_of_v2_account",
        http_status=400)


class FakeTable:
    def __init__(self) -> None:
        self.rows: dict[str, str] = {}
        self.user_id = ""
        self.pending: dict[str, str] | None = None

    def select(self, _columns: str):
        return self

    def eq(self, _column: str, user_id: str):
        self.user_id = user_id
        return self

    def limit(self, _limit: int):
        return self

    def insert(self, row: dict[str, str]):
        self.pending = row
        return self

    def execute(self):
        if self.pending is not None:
            row = self.pending
            self.pending = None
            if row["creator_user_id"] in self.rows:
                raise RuntimeError("duplicate")
            self.rows[row["creator_user_id"]] = row["stripe_account_id"]
            return SimpleNamespace(data=[row])
        account_id = self.rows.get(self.user_id)
        return SimpleNamespace(data=[{"stripe_account_id": account_id}] if account_id else [])


class FakeUsers:
    """public.users: the creator's login email (Stripe needs it at account creation)."""

    def __init__(self) -> None:
        self.emails: dict[str, str | None] = {}
        self.reads: list[str] = []
        self.user_id = ""

    def select(self, _columns: str):
        return self

    def eq(self, _column: str, user_id: str):
        self.user_id = user_id
        return self

    def limit(self, _limit: int):
        return self

    def execute(self):
        self.reads.append(self.user_id)
        email = self.emails.get(self.user_id, "sofia@example.com")
        return SimpleNamespace(data=[{"email": email}] if email is not None else [])


class FakeStripe:
    """raw_request for the v2 endpoints, v1 services for v1-created accounts."""

    def __init__(self) -> None:
        self.created: list[tuple[dict, dict]] = []      # (v2 account body, request options)
        self.links: list[dict] = []                     # v2 account link bodies
        self.v1_links: list[dict] = []
        self.retrieved: list[str] = []                  # v2 account reads
        self.v1_retrieved: list[str] = []
        self.v1_accounts: dict[str, dict] = {}          # ids created through v1, with their v1 state
        self.v2_state: dict[str, dict] = {}
        self.link_url = "https://connect.stripe.com/setup/c/test"
        self.v1 = SimpleNamespace(
            accounts=SimpleNamespace(create=self._v1_create, retrieve=self._v1_retrieve),
            account_links=SimpleNamespace(create=self._v1_link),
        )

    def raw_request(self, method: str, path: str, **params: Any):
        options = {k: params.pop(k) for k in ("stripe_version", "idempotency_key") if k in params}
        assert options.get("stripe_version") == V2_VERSION
        if method == "post" and path == "/v2/core/accounts":
            self.created.append((params, options))
            return SimpleNamespace(data={"id": "acct_testcreator", "object": "v2.core.account"})
        if method == "post" and path == "/v2/core/account_links":
            self.links.append(params)
            return SimpleNamespace(data={"object": "v2.core.account_link", "url": self.link_url})
        if method == "get" and path.startswith("/v2/core/accounts/"):
            account_id = path.rsplit("/", 1)[1]
            assert params == {"include[0]": "configuration.recipient"}
            self.retrieved.append(account_id)
            if account_id in self.v1_accounts:
                raise _v1_account_error()
            return SimpleNamespace(data=self.v2_state.get(account_id, _v2_account()))
        raise AssertionError(f"unexpected {method} {path}")

    def _v1_create(self, *_a: Any, **_k: Any):
        raise AssertionError("Accounts v1 creation must never be called")

    def _v1_retrieve(self, account_id: str):
        self.v1_retrieved.append(account_id)
        return self.v1_accounts[account_id]

    def _v1_link(self, params: dict):
        self.v1_links.append(params)
        return {"url": "https://connect.stripe.com/setup/e/v1"}


@pytest.fixture()
def backend(monkeypatch):
    table = FakeTable()
    table.users = FakeUsers()
    fake = FakeStripe()
    monkeypatch.setattr(
        creator_payouts.supabase_client,
        "get_service_client",
        lambda: SimpleNamespace(table=lambda name: {creator_payouts.TABLE: table, "users": table.users}.get(name)),
    )
    monkeypatch.setattr(creator_payouts, "get_stripe_client", lambda: fake)
    monkeypatch.setattr(
        creator_payouts,
        "get_settings",
        lambda: SimpleNamespace(
            stripe_secret_key="sk_test_not_real",
            public_app_url="https://www.babyg.ai",
            app_url="http://localhost:8000",
            is_production=True,
        ),
    )
    table.fake = fake
    return table, fake


# ================================================================ v2 creation


def test_creator_account_created_once_with_accounts_v2_and_owned_by_session_id(backend):
    table, fake = backend
    uid = str(uuid4())
    first = creator_payouts.onboarding_url(uid, create_if_missing=True)
    second = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert first == second == "https://connect.stripe.com/setup/c/test"
    assert table.rows == {uid: "acct_testcreator"}
    [(body, options)] = fake.created
    assert body == EXPECTED_V2_ACCOUNT
    assert options["stripe_version"] == V2_VERSION
    assert options["idempotency_key"].startswith("babyg-creator-account-v2-")
    assert options["idempotency_key"].endswith(uid)
    expected_link = {
        "account": "acct_testcreator",
        "use_case": {
            "type": "account_onboarding",
            "account_onboarding": {
                "return_url": "https://www.babyg.ai/creator/payouts/return",
                "refresh_url": "https://www.babyg.ai/creator/payouts/refresh",
            },
        },
    }
    assert fake.links == [expected_link, expected_link]
    assert fake.v1_links == [] and fake.v1_retrieved == []


def test_refresh_reuses_mapping_and_requires_existing_account(backend):
    table, fake = backend
    uid = str(uuid4())
    with pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=False)
    table.rows[uid] = "acct_existing"
    assert creator_payouts.onboarding_url(uid, create_if_missing=False).startswith("https://connect.stripe.com/")
    assert fake.created == []
    assert fake.links[0]["account"] == "acct_existing"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (_v2_account("active", "active"), "ready"),
        (_v2_account("active", None), "ready"),          # Stripe reports no payouts capability
        (_v2_account("pending", "active"), "incomplete"),
        (_v2_account("active", "restricted"), "incomplete"),
        (_v2_account("active", "active", applied=False), "incomplete"),
        ({"id": "acct_x", "object": "v2.core.account"}, "incomplete"),
    ],
)
def test_status_reads_the_v2_recipient_configuration(backend, state, expected):
    table, fake = backend
    uid = str(uuid4())
    assert creator_payouts.payout_status(uid) == "not_set_up"
    table.rows[uid] = "acct_existing"
    fake.v2_state["acct_existing"] = state
    assert creator_payouts.payout_status(uid) == expected
    assert creator_payouts.ready_account_id(uid) == ("acct_existing" if expected == "ready" else None)
    assert set(fake.retrieved) == {"acct_existing"} and fake.v1_retrieved == []


def test_stripe_failures_and_untrusted_link_fail_closed(backend, monkeypatch):
    table, fake = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_existing"
    for bad in ("https://evil.example/", "http://connect.stripe.com/x", "https://stripe.com.evil.example/",
                "javascript:alert(1)", None):
        fake.link_url = bad
        with pytest.raises(creator_payouts.PayoutSetupError, match="unavailable"):
            creator_payouts.onboarding_url(uid, create_if_missing=True)

    def boom(*_a: Any, **_k: Any):
        raise stripe.APIConnectionError("down")

    monkeypatch.setattr(fake, "raw_request", boom)
    assert creator_payouts.payout_status(uid) == "unavailable"
    monkeypatch.setattr(creator_payouts, "get_settings", lambda: SimpleNamespace(stripe_secret_key="sk_live_secret"))
    with pytest.raises(creator_payouts.PayoutSetupError, match="unavailable") as exc:
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert "sk_live_secret" not in str(exc.value)


def test_stored_account_ids_are_validated_before_use(backend):
    table, fake = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_x/../../v1/charges"
    assert creator_payouts.payout_status(uid) == "unavailable"
    with pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert fake.retrieved == [] and fake.links == []


# ====================================================== existing v1 accounts


def test_existing_v1_created_accounts_keep_v1_reads_and_v1_onboarding_links(backend):
    """Stripe answers a v2 read of a v1-created account with
    v1_account_instead_of_v2_account; those accounts stay on v1."""
    table, fake = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_legacyv1"
    fake.v1_accounts["acct_legacyv1"] = {"payouts_enabled": True, "capabilities": {"transfers": "active"}}
    assert creator_payouts.payout_status(uid) == "ready"
    assert creator_payouts.ready_account_id(uid) == "acct_legacyv1"
    fake.v1_accounts["acct_legacyv1"] = {"payouts_enabled": False, "capabilities": {"transfers": "active"}}
    assert creator_payouts.payout_status(uid) == "incomplete"
    url = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert url == "https://connect.stripe.com/setup/e/v1"
    assert fake.v1_links == [{
        "account": "acct_legacyv1",
        "type": "account_onboarding",
        "return_url": "https://www.babyg.ai/creator/payouts/return",
        "refresh_url": "https://www.babyg.ai/creator/payouts/refresh",
    }]
    assert fake.links == [] and fake.created == []


# ===================================================================== routes


def _session(client: TestClient, role: str = "creator") -> str:
    uid = str(uuid4())
    response = Response()
    write_session(response, {"user_id": uid, "role": role})
    cookie = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return uid


def test_routes_require_creator_and_do_not_accept_identity(backend, monkeypatch):
    client = TestClient(app, follow_redirects=False)
    assert client.post("/creator/payouts/start").status_code == 401
    assert client.get("/creator/payouts/refresh").headers["location"].startswith("/auth/login")
    assert client.get("/creator/payouts/return").headers["location"].startswith("/auth/login")
    _session(client, "brand")
    assert client.post("/creator/payouts/start").status_code == 403
    uid = _session(client)
    response = client.post(
        "/creator/payouts/start?creator_user_id=spoofed&stripe_account_id=acct_spoofed",
        data={"creator_user_id": "spoofed", "stripe_account_id": "acct_spoofed",
              "contact_email": "attacker@example.com"},
    )
    table, fake = backend
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://connect.stripe.com/")
    assert table.rows == {uid: "acct_testcreator"}
    assert fake.links[0]["account"] == "acct_testcreator"
    assert fake.created[0][0]["contact_email"] == "sofia@example.com"   # from public.users, not the form
    assert table.users.reads == [uid]
    assert client.get("/creator/payouts/refresh?account=acct_spoofed").status_code == 303
    returned = client.get("/creator/payouts/return?account=acct_spoofed&next=https://evil.example")
    assert returned.headers["location"] == "/creator/profile/settings#payouts"


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("not_set_up", "set up payouts"),
        ("incomplete", "continue setup"),
        ("ready", "ready to receive payments"),
    ],
)
def test_settings_shows_authoritative_payout_state(state, expected, monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _session(client)
    monkeypatch.setattr(
        profiles, "get_creator_profile_cached",
        lambda _uid, _req: {"onboarding_completed_at": "2026-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(oauth_connections, "get_google_connection", lambda _uid: None)
    monkeypatch.setattr(oauth_connections, "get_instagram_connection", lambda _uid: None)
    monkeypatch.setattr(oauth_connections, "google_calendar_connected", lambda _row: False)
    monkeypatch.setattr(oauth_connections, "google_gmail_connected", lambda _row: False)
    monkeypatch.setattr(agent_memory, "load", lambda _uid: None)
    monkeypatch.setattr(agent_memory, "history", lambda _uid, limit=40: [])
    monkeypatch.setattr(creator_payouts, "payout_status", lambda _uid: state)
    response = client.get("/creator/profile/settings?payouts=returned")
    assert response.status_code == 200
    assert expected in response.text
    assert 'id="payouts"' in response.text
    assert "acct_" not in response.text
    assert "sk_test_" not in response.text


# ===================================================== REAL SDK wire contract
#
# The REAL Stripe SDK (12.5.1) runs against a local HTTP stand-in for
# api.stripe.com that enforces Stripe's documented rules, so the exact bytes
# on the wire are verified: v2 JSON bodies + the v2 API version header, v1
# form bodies + the SDK's pinned v1 version for v1-created accounts.

CONTRACT_KEY = "sk_test_contract_key_must_never_be_logged"

# The authoritative production response (Railway, stage='account_create').
PRODUCTION_MESSAGE = (
    "Stripe no longer recommends Accounts v1 for new Connect integrations. Create connected "
    "accounts with POST /v2/core/accounts instead. If you are required to use Accounts v1, "
    "[set once at account creation]: an account email address for the account, required "
    "whenever a 'recipient' configuration is used, and controller[fees][payer] and "
    "controller[losses][payments]."
)
V1_REJECTION = (400, {"error": {"type": "invalid_request_error", "message": PRODUCTION_MESSAGE}})
# The authoritative production response to fa10b90's v2 request (Railway,
# stage='account_create', stripe_code='identity_country_required').
PRODUCTION_COUNTRY_MESSAGE = "The field identity.country is required before setting configuration.recipient"


def _v2_error(code: str, message: str) -> tuple[int, dict]:
    return 400, {"error": {"type": "invalid_request_error", "code": code, "message": message}}


def stripe_v2_account_rule(req: dict) -> tuple[int, dict]:
    """POST /v2/core/accounts as documented for 2026-09-30.endive."""
    body = req["json"]
    if req["headers"].get("stripe-version") != V2_VERSION:
        return _v2_error("invalid_api_version", "v2 requires an explicit v2 API version")
    if "type" in body or "controller" in body or "email" in body or "capabilities" in body:
        return _v2_error("parameter_unknown", "v1 account parameter sent to /v2/core/accounts")
    if not body.get("contact_email"):
        return _v2_error("email_invalid", "An account email address is required for a recipient configuration")
    country = (body.get("identity") or {}).get("country")
    if "recipient" in (body.get("configuration") or {}) and not country:
        return _v2_error("identity_country_required", PRODUCTION_COUNTRY_MESSAGE)
    if not (isinstance(country, str) and len(country) == 2 and country.isalpha()):
        return _v2_error("parameter_invalid", "identity.country must be an ISO 3166-1 alpha-2 code")
    resp = (body.get("defaults") or {}).get("responsibilities") or {}
    if set(resp) != {"fees_collector", "losses_collector"}:
        return _v2_error("parameter_missing", "defaults.responsibilities needs fees_collector and losses_collector")
    if body.get("dashboard") == "express" and (resp["fees_collector"], resp["losses_collector"]) != (
            "application", "application"):
        return _v2_error("account_controller_express_dash_without_application_losses_or_fees",
                         "If `dashboard` is `express`, `fees_collector` must be `application` and "
                         "`losses_collector` must be `application`.")
    transfers = (((body.get("configuration") or {}).get("recipient") or {}).get("capabilities") or {}).get(
        "stripe_balance", {}).get("stripe_transfers") or {}
    if transfers.get("requested") is not True:
        return _v2_error("configuration_creation_invalid", "recipient stripe_transfers not requested")
    return 200, {"id": "acct_v2creator", "object": "v2.core.account", "dashboard": "express"}


def stripe_v2_link_rule(req: dict) -> tuple[int, dict]:
    body = req["json"]
    use = body.get("use_case") or {}
    onboarding = use.get("account_onboarding") or {}
    if req["headers"].get("stripe-version") != V2_VERSION or not body.get("account") \
            or use.get("type") != "account_onboarding" or not onboarding.get("refresh_url") \
            or "configurations" in onboarding:
        return _v2_error("parameter_invalid", "bad v2 account link")
    return 200, {"object": "v2.core.account_link", "account": body["account"],
                 "url": f"https://connect.stripe.com/d/setup/e/{body['account']}/x"}


class _StubStripe:
    """A local stand-in for api.stripe.com that records each request."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self, method: str) -> None:
                raw = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                parts = urlsplit(self.path)
                headers = {k.lower(): v for k, v in self.headers.items()}
                is_json = headers.get("content-type", "").startswith("application/json")
                req = {"method": method, "path": parts.path, "query": parts.query, "headers": headers,
                       "raw": raw, "json": json.loads(raw) if is_json and raw else None,
                       "form": parse_qs(raw) if raw and not is_json else {}}
                stub.requests.append(req)
                route = stub.routes.get(f"{method} {parts.path}")
                status, payload = (route(req) if callable(route) else route) if route else (
                    404, {"error": {"type": "invalid_request_error", "code": "not_found", "message": "no route"}})
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("request-id", f"req_stub_{len(stub.requests)}")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):  # noqa: N802 (http.server API)
                self._serve("POST")

            def do_GET(self):  # noqa: N802
                self._serve("GET")

            def log_message(self, *_args):
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def client(self) -> stripe.StripeClient:
        port = self.server.server_address[1]
        return stripe.StripeClient(
            api_key=CONTRACT_KEY,
            base_addresses={"api": f"http://127.0.0.1:{port}"},
            max_network_retries=0,
        )

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


V2_OK = {"POST /v2/core/accounts": stripe_v2_account_rule, "POST /v2/core/account_links": stripe_v2_link_rule,
         "POST /v1/accounts": V1_REJECTION}


@pytest.fixture()
def real_sdk(backend, monkeypatch):
    stubs: list[_StubStripe] = []

    def make(routes: dict[str, Any]) -> _StubStripe:
        stub = _StubStripe(routes)
        stubs.append(stub)
        monkeypatch.setattr(creator_payouts, "get_stripe_client", stub.client)
        return stub

    yield make
    for stub in stubs:
        stub.close()


def test_stub_reproduces_the_exact_production_failure_of_accounts_v1(backend, real_sdk):
    """Both previous request shapes (11c8a22's type=express and f9d06ac's
    controller+email) get Stripe's production 400 from the stand-in."""
    client = real_sdk(V2_OK).client()
    for params in ({"type": "express", "capabilities": {"transfers": {"requested": True}}},
                   {"controller": {"fees": {"payer": "application"}, "losses": {"payments": "application"},
                                   "requirement_collection": "stripe", "stripe_dashboard": {"type": "express"}},
                    "email": "sofia@example.com", "capabilities": {"transfers": {"requested": True}}}):
        with pytest.raises(stripe.InvalidRequestError) as exc:
            client.v1.accounts.create(params)
        assert exc.value.http_status == 400 and exc.value.code is None
        assert exc.value.user_message == PRODUCTION_MESSAGE
        assert getattr(exc.value.error, "type", None) == "invalid_request_error"


def test_real_sdk_onboarding_uses_accounts_v2_and_never_v1_create(backend, real_sdk, caplog):
    table = backend[0]
    stub = real_sdk(V2_OK)
    uid = str(uuid4())
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"):
        url = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert url == "https://connect.stripe.com/d/setup/e/acct_v2creator/x"
    assert table.rows == {uid: "acct_v2creator"}
    create, link = stub.requests
    assert (create["method"], create["path"], link["method"], link["path"]) == (
        "POST", "/v2/core/accounts", "POST", "/v2/core/account_links")
    assert create["headers"]["content-type"].startswith("application/json")
    assert create["json"] == EXPECTED_V2_ACCOUNT
    assert create["headers"]["stripe-version"] == V2_VERSION
    assert create["headers"]["authorization"] == f"Bearer {CONTRACT_KEY}"
    assert create["headers"]["idempotency-key"].startswith("babyg-creator-account-v2-")
    assert create["headers"]["idempotency-key"].endswith(uid)
    assert link["json"] == {"account": "acct_v2creator", "use_case": {
        "type": "account_onboarding",
        "account_onboarding": {"return_url": "https://www.babyg.ai/creator/payouts/return",
                               "refresh_url": "https://www.babyg.ai/creator/payouts/refresh"}}}
    assert link["headers"]["stripe-version"] == V2_VERSION
    assert not [r for r in stub.requests if r["path"] == "/v1/accounts"]
    assert not [r for r in caplog.records if r.name == "app.services.creator_payouts"]


def test_real_sdk_readiness_reads_v2_with_include_and_v1_accounts_fall_back(backend, real_sdk):
    table = backend[0]
    v2_ready = {"id": "acct_v2ready", "object": "v2.core.account",
                "configuration": {"recipient": {"applied": True, "capabilities": {"stripe_balance": {
                    "stripe_transfers": {"status": "active", "status_details": []},
                    "payouts": {"status": "active", "status_details": []}}}}}}
    routes = dict(V2_OK)
    routes["GET /v2/core/accounts/acct_v2ready"] = (200, v2_ready)
    routes["GET /v2/core/accounts/acct_legacyv1"] = _v2_error(
        "v1_account_instead_of_v2_account", "V1 Account ID cannot be used in V2 Account APIs.")
    routes["GET /v1/accounts/acct_legacyv1"] = (200, {"id": "acct_legacyv1", "object": "account",
                                                     "payouts_enabled": True,
                                                     "capabilities": {"transfers": "active"}})
    routes["POST /v1/account_links"] = (200, {"object": "account_link", "url": "https://connect.stripe.com/setup/e/v1"})
    stub = real_sdk(routes)
    v2_uid, v1_uid = str(uuid4()), str(uuid4())
    table.rows.update({v2_uid: "acct_v2ready", v1_uid: "acct_legacyv1"})
    assert creator_payouts.readiness(v2_uid) == ("ready", "acct_v2ready")
    assert creator_payouts.readiness(v1_uid) == ("ready", "acct_legacyv1")
    v2_get, v1_probe, v1_get = stub.requests
    assert parse_qs(v2_get["query"]) == {"include[0]": ["configuration.recipient"]}   # indexed, as current SDKs send
    assert v2_get["headers"]["stripe-version"] == V2_VERSION
    assert (v1_get["method"], v1_get["path"]) == ("GET", "/v1/accounts/acct_legacyv1")
    # v1 calls keep the SDK's pinned v1 version (unchanged for Step 7A)
    assert v1_get["headers"]["stripe-version"] == stripe.api_version == "2025-08-27.basil"
    stub.requests.clear()
    assert creator_payouts.onboarding_url(v1_uid, create_if_missing=True) == "https://connect.stripe.com/setup/e/v1"
    assert [(r["method"], r["path"]) for r in stub.requests] == [
        ("GET", "/v2/core/accounts/acct_legacyv1"), ("POST", "/v1/account_links")]
    assert stub.requests[1]["form"]["type"] == ["account_onboarding"]


@pytest.mark.parametrize(
    ("route", "stage", "code"),
    [("POST /v2/core/accounts", "account_create", "account_controller_express_dash_without_application_losses_or_fees"),
     ("POST /v2/core/account_links", "account_link", "accounts_v2_access_blocked")],
)
def test_stripe_rejection_is_logged_with_stage_and_reason_never_the_key(
    backend, real_sdk, caplog, route, stage, code
):
    reason = "Stripe says no for a documented reason."
    routes = dict(V2_OK)
    routes[route] = _v2_error(code, reason)
    real_sdk(routes)
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"), \
            pytest.raises(creator_payouts.PayoutSetupError, match="^Payout setup is unavailable$"):
        creator_payouts.onboarding_url(str(uuid4()), create_if_missing=True)
    [record] = [r for r in caplog.records if r.name == "app.services.creator_payouts"]
    line = record.getMessage()
    assert f"stage='{stage}'" in line
    assert "error='InvalidRequestError'" in line and "http_status=400" in line
    assert "stripe_type='invalid_request_error'" in line and f"stripe_code='{code}'" in line
    assert "request_id='req_stub_" in line and reason in line
    assert "key_kind=" in line
    assert CONTRACT_KEY not in line and "sk_test_contract" not in line


def test_email_is_read_from_the_creator_row_and_masked_in_logs(backend, real_sdk, caplog):
    table = backend[0]
    uid = str(uuid4())
    table.users.emails[uid] = "maya.lee@example.com"
    routes = dict(V2_OK)
    routes["POST /v2/core/accounts"] = _v2_error("email_invalid", "Incorrect email maya.lee@example.com")
    stub = real_sdk(routes)
    with caplog.at_level(logging.WARNING), pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert table.users.reads == [uid]
    assert stub.requests[0]["json"]["contact_email"] == "maya.lee@example.com"
    assert "maya.lee@example.com" not in caplog.text and "[email]" in caplog.text


@pytest.mark.parametrize("email", [None, "", "not-an-email"])
def test_missing_email_fails_closed_before_any_stripe_call(backend, email, caplog):
    table, fake = backend
    uid = str(uuid4())
    table.users.emails[uid] = email
    with caplog.at_level(logging.WARNING), pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert fake.created == [] and table.rows == {}
    assert "stage='account_email'" in caplog.text


def test_existing_accounts_and_refresh_never_read_email_or_create(backend):
    table, fake = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_existing"
    creator_payouts.onboarding_url(uid, create_if_missing=True)
    creator_payouts.onboarding_url(uid, create_if_missing=False)
    assert fake.created == [] and table.users.reads == []
    assert [link["account"] for link in fake.links] == ["acct_existing", "acct_existing"]


def test_config_and_origin_failures_name_their_stage(backend, monkeypatch, caplog):
    monkeypatch.setattr(
        creator_payouts, "get_settings",
        lambda: SimpleNamespace(stripe_secret_key="rk_test_restricted_secret"),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(str(uuid4()), create_if_missing=True)
    assert "stage='config'" in caplog.text and "rk_test_restricted_secret" not in caplog.text
    caplog.clear()
    monkeypatch.setattr(
        creator_payouts, "get_settings",
        lambda: SimpleNamespace(stripe_secret_key="sk_test_x", public_app_url="http://www.babyg.ai",
                                app_url="", is_production=True),
    )
    with caplog.at_level(logging.WARNING), pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(str(uuid4()), create_if_missing=True)
    assert "stage='origin'" in caplog.text and "origin_scheme='http'" in caplog.text
    assert caplog.text.count("creator_payouts.failed") == 1


def test_status_failure_is_logged_not_silent(backend, monkeypatch, caplog):
    table, fake = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_existing"

    def boom(*_a: Any, **_k: Any):
        raise stripe.PermissionError("This application does not have access", http_status=403)

    monkeypatch.setattr(fake, "raw_request", boom)
    with caplog.at_level(logging.WARNING):
        assert creator_payouts.payout_status(uid) == "unavailable"
        assert creator_payouts.ready_account_id(uid) is None
    assert "stage='status_account_retrieve'" in caplog.text and "http_status=403" in caplog.text


def test_failed_create_is_not_replayed_for_24_hours(backend, monkeypatch):
    """Stripe caches a failed create under its idempotency key for 24h, so a
    fixed per-creator key kept failing after the cause was fixed. Same window
    -> same key (double-submit safe); next window -> a fresh attempt."""
    uid = str(uuid4())
    clock = {"now": 1_000_000.0}
    monkeypatch.setattr(creator_payouts.time, "time", lambda: clock["now"])
    first = creator_payouts._idempotency_key(uid)
    window = creator_payouts.IDEMPOTENCY_WINDOW_SECONDS
    clock["now"] += window - 1 - (clock["now"] % window)
    assert creator_payouts._idempotency_key(uid) == first
    clock["now"] += 2
    assert creator_payouts._idempotency_key(uid) != first
    assert creator_payouts._idempotency_key(uid).endswith(uid)
    assert creator_payouts._idempotency_key(str(uuid4())) != creator_payouts._idempotency_key(uid)


def test_safe_error_fields_mask_credentials():
    from app.services import stripe_client

    fields = stripe_client.safe_error_fields(
        RuntimeError("Invalid API Key provided: sk_live_abc123 and whsec_zzz")
    )
    assert "sk_live_abc123" not in fields["message"] and "whsec_zzz" not in fields["message"]
    assert fields["error"] == "RuntimeError"


def test_long_stripe_messages_are_logged_whole():
    """Production's ~360-char message was cut at 300 chars, hiding the
    required field names. The whole message (masked) must reach the log."""
    from app.services import stripe_client

    fields = stripe_client.safe_error_fields(RuntimeError(PRODUCTION_MESSAGE))
    assert fields["message"] == PRODUCTION_MESSAGE
    assert stripe_client.safe_error_fields(RuntimeError("x" * 5000))["message"] == "x" * 1000


def test_v2_request_matches_stripes_published_contract():
    """Pins the request to the 2026-09-30.endive v2 contract: recipient
    configuration (destination charges without on_behalf_of), Express
    Dashboard with application fees + losses (Stripe's documented rule for
    an Express Dashboard), and nothing from Accounts v1."""
    body = creator_payouts._new_account_request("a@b.co")
    assert body == {**EXPECTED_V2_ACCOUNT, "contact_email": "a@b.co"}
    assert creator_payouts.ACCOUNTS_V2_API_VERSION == V2_VERSION
    assert not {"type", "controller", "email", "capabilities", "business_type"} & set(body)
    # US-only creator payouts (deals and payments are USD-only); only the
    # country is set -- entity type and the rest come from hosted onboarding.
    assert body["identity"] == {"country": "us"} and creator_payouts.PAYOUT_COUNTRY == "us"


def test_stub_reproduces_the_production_identity_country_rejection(backend, real_sdk):
    """fa10b90's v2 request (recipient configuration, no identity.country)
    gets exactly the production 400 from the stand-in; the corrected request
    passes the same rule."""
    stub = real_sdk(V2_OK)
    client = stub.client()
    previous = {k: v for k, v in EXPECTED_V2_ACCOUNT.items() if k != "identity"}
    with pytest.raises(stripe.InvalidRequestError) as exc:
        client.raw_request("post", "/v2/core/accounts", **previous, stripe_version=V2_VERSION)
    assert exc.value.http_status == 400
    assert exc.value.code == "identity_country_required"
    assert exc.value.user_message == PRODUCTION_COUNTRY_MESSAGE
    corrected = creator_payouts._new_account_request("sofia@example.com")
    response = client.raw_request("post", "/v2/core/accounts", **corrected, stripe_version=V2_VERSION)
    assert response.data["id"] == "acct_v2creator"


def test_onboarding_sends_identity_country_and_passes_the_production_rule(backend, real_sdk, caplog):
    table = backend[0]
    stub = real_sdk(V2_OK)
    uid = str(uuid4())
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"):
        url = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert url.startswith("https://connect.stripe.com/")
    assert table.rows == {uid: "acct_v2creator"}
    create = stub.requests[0]
    assert create["path"] == "/v2/core/accounts"
    assert create["json"]["identity"] == {"country": "us"}
    assert "recipient" in create["json"]["configuration"]
    assert not [r for r in caplog.records if r.name == "app.services.creator_payouts"]


def test_identity_country_rejection_is_logged_with_its_stripe_code(backend, real_sdk, caplog):
    routes = dict(V2_OK)
    routes["POST /v2/core/accounts"] = _v2_error("identity_country_required", PRODUCTION_COUNTRY_MESSAGE)
    real_sdk(routes)
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"), \
            pytest.raises(creator_payouts.PayoutSetupError, match="^Payout setup is unavailable$"):
        creator_payouts.onboarding_url(str(uuid4()), create_if_missing=True)
    line = caplog.records[-1].getMessage()
    assert "stage='account_create'" in line and "stripe_code='identity_country_required'" in line
    assert PRODUCTION_COUNTRY_MESSAGE in line and backend[0].rows == {}
