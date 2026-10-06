"""Sandbox Connect onboarding and creator-only Settings contract."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import agent_memory, creator_payouts, oauth_connections, profiles


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
        self.emails: dict[str, str] = {}
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


@pytest.fixture()
def backend(monkeypatch):
    table = FakeTable()
    table.users = FakeUsers()
    created: list[tuple[dict, dict]] = []
    links: list[dict] = []
    retrieved: list[str] = []

    def create(params, options):
        created.append((params, options))
        return {"id": "acct_testcreator"}

    def link(params):
        links.append(params)
        return {"url": "https://connect.stripe.com/setup/c/test"}

    def retrieve(account_id):
        retrieved.append(account_id)
        return {"payouts_enabled": True, "capabilities": {"transfers": "active"}}

    stripe = SimpleNamespace(
        v1=SimpleNamespace(
            accounts=SimpleNamespace(create=create, retrieve=retrieve),
            account_links=SimpleNamespace(create=link),
        )
    )
    monkeypatch.setattr(
        creator_payouts.supabase_client,
        "get_service_client",
        lambda: SimpleNamespace(table=lambda name: {creator_payouts.TABLE: table, "users": table.users}.get(name)),
    )
    monkeypatch.setattr(creator_payouts, "get_stripe_client", lambda: stripe)
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
    return table, created, links, retrieved


def test_creator_account_created_once_and_owned_by_session_id(backend):
    table, created, links, _ = backend
    uid = str(uuid4())
    first = creator_payouts.onboarding_url(uid, create_if_missing=True)
    second = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert first == second == "https://connect.stripe.com/setup/c/test"
    assert table.rows == {uid: "acct_testcreator"}
    assert len(created) == 1
    assert created[0][0] == {
        "controller": {
            "fees": {"payer": "application"},
            "losses": {"payments": "application"},
            "requirement_collection": "stripe",
            "stripe_dashboard": {"type": "express"},
        },
        "email": "sofia@example.com",
        "capabilities": {"transfers": {"requested": True}},
    }
    assert "type" not in created[0][0]
    assert created[0][1]["idempotency_key"].endswith(uid)
    assert links[0] == {
        "account": "acct_testcreator",
        "type": "account_onboarding",
        "return_url": "https://www.babyg.ai/creator/payouts/return",
        "refresh_url": "https://www.babyg.ai/creator/payouts/refresh",
    }
    assert len(links) == 2


def test_refresh_reuses_mapping_and_requires_existing_account(backend):
    table, created, links, _ = backend
    uid = str(uuid4())
    with pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=False)
    table.rows[uid] = "acct_existing"
    assert creator_payouts.onboarding_url(uid, create_if_missing=False).startswith("https://connect.stripe.com/")
    assert not created
    assert links[0]["account"] == "acct_existing"


def test_status_uses_stripe_not_browser_return(backend, monkeypatch):
    table, _, _, retrieved = backend
    uid = str(uuid4())
    assert creator_payouts.payout_status(uid) == "not_set_up"
    table.rows[uid] = "acct_existing"
    assert creator_payouts.payout_status(uid) == "ready"
    assert retrieved == ["acct_existing"]
    monkeypatch.setattr(
        creator_payouts,
        "get_stripe_client",
        lambda: SimpleNamespace(
            v1=SimpleNamespace(accounts=SimpleNamespace(retrieve=lambda _id: {"payouts_enabled": False}))
        ),
    )
    assert creator_payouts.payout_status(uid) == "incomplete"


def test_stripe_failures_and_untrusted_link_fail_closed(backend, monkeypatch):
    table, _, _, _ = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_existing"
    monkeypatch.setattr(
        creator_payouts,
        "get_stripe_client",
        lambda: SimpleNamespace(
            v1=SimpleNamespace(
                account_links=SimpleNamespace(create=lambda _params: {"url": "https://evil.example/"})
            )
        ),
    )
    with pytest.raises(creator_payouts.PayoutSetupError, match="unavailable"):
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert creator_payouts.payout_status(uid) == "unavailable"
    monkeypatch.setattr(
        creator_payouts,
        "get_settings",
        lambda: SimpleNamespace(stripe_secret_key="sk_live_secret"),
    )
    with pytest.raises(creator_payouts.PayoutSetupError, match="unavailable") as exc:
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert "sk_live_secret" not in str(exc.value)


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
        data={"creator_user_id": "spoofed", "stripe_account_id": "acct_spoofed"},
    )
    table, _, links, _ = backend
    assert response.status_code == 303
    assert response.headers["location"].startswith("https://connect.stripe.com/")
    assert table.rows == {uid: "acct_testcreator"}
    assert links[0]["account"] == "acct_testcreator"
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


# --------------------------------------------------------------------------
# Phase 1 (Step 7A): production "couldn't open payout setup" diagnosis.
# Every failure used to be swallowed without a log line, so the cause could
# not be read anywhere. These pin the stage-level, secret-free logging and
# the idempotency window, and run the REAL Stripe SDK against a local HTTP
# stub so the exact wire format of both onboarding calls is verified.
# --------------------------------------------------------------------------

import json  # noqa: E402
import logging  # noqa: E402
import threading  # noqa: E402
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402
from urllib.parse import parse_qs  # noqa: E402

import stripe  # noqa: E402

CONTRACT_KEY = "sk_test_contract_key_must_never_be_logged"


class _StubStripe:
    """A local stand-in for api.stripe.com that records each request."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802 (http.server API)
                body = self.rfile.read(int(self.headers.get("content-length") or 0)).decode()
                form = parse_qs(body)
                stub.requests.append({"path": self.path, "form": form, "headers": dict(self.headers)})
                route = stub.routes[self.path]
                status, payload = route(form) if callable(route) else route
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("request-id", f"req_stub_{len(stub.requests)}")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

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


def test_real_sdk_wire_contract_for_account_and_hosted_link(backend, real_sdk):
    table = backend[0]
    stub = real_sdk({
        "/v1/accounts": (200, {"id": "acct_stubcreator", "object": "account"}),
        "/v1/account_links": (200, {"object": "account_link",
                                    "url": "https://connect.stripe.com/setup/e/acct_stubcreator/x"}),
    })
    uid = str(uuid4())
    url = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert url == "https://connect.stripe.com/setup/e/acct_stubcreator/x"
    assert table.rows == {uid: "acct_stubcreator"}
    create, link = stub.requests
    assert create["path"] == "/v1/accounts"
    assert create["form"] == {
        "controller[fees][payer]": ["application"],
        "controller[losses][payments]": ["application"],
        "controller[requirement_collection]": ["stripe"],
        "controller[stripe_dashboard][type]": ["express"],
        "email": ["sofia@example.com"],
        "capabilities[transfers][requested]": ["true"],
    }
    headers = {k.lower(): v for k, v in create["headers"].items()}
    assert headers["authorization"] == f"Bearer {CONTRACT_KEY}"
    assert headers["stripe-version"] == stripe.api_version
    assert headers["idempotency-key"].startswith("babyg-creator-connect-")
    assert headers["idempotency-key"].endswith(uid)
    assert link["path"] == "/v1/account_links"
    assert link["form"] == {
        "account": ["acct_stubcreator"],
        "type": ["account_onboarding"],
        "return_url": ["https://www.babyg.ai/creator/payouts/return"],
        "refresh_url": ["https://www.babyg.ai/creator/payouts/refresh"],
    }


@pytest.mark.parametrize(
    ("path", "stage"),
    [("/v1/accounts", "account_create"), ("/v1/account_links", "account_link")],
)
def test_stripe_rejection_is_logged_with_stage_and_reason_never_the_key(
    backend, real_sdk, caplog, path, stage
):
    reason = "Please review the responsibilities of managing losses for connected accounts."
    routes = {
        "/v1/accounts": (200, {"id": "acct_stubcreator", "object": "account"}),
        "/v1/account_links": (200, {"url": "https://connect.stripe.com/x"}),
    }
    routes[path] = (400, {"error": {"type": "invalid_request_error", "code": "platform_setup_required",
                                    "message": reason}})
    real_sdk(routes)
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"), \
            pytest.raises(creator_payouts.PayoutSetupError, match="^Payout setup is unavailable$"):
        creator_payouts.onboarding_url(str(uuid4()), create_if_missing=True)
    [record] = [r for r in caplog.records if r.name == "app.services.creator_payouts"]
    line = record.getMessage()
    assert f"stage='{stage}'" in line
    assert "error='InvalidRequestError'" in line and "http_status=400" in line
    assert "stripe_type='invalid_request_error'" in line
    assert "stripe_code='platform_setup_required'" in line
    assert "request_id='req_stub_" in line and reason in line
    assert "key_kind=" in line
    assert CONTRACT_KEY not in line and "sk_test_contract" not in line


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
    table = backend[0]
    uid = str(uuid4())
    table.rows[uid] = "acct_existing"

    def boom(_account_id):
        raise stripe.PermissionError("This application does not have access", http_status=403)

    monkeypatch.setattr(
        creator_payouts, "get_stripe_client",
        lambda: SimpleNamespace(v1=SimpleNamespace(accounts=SimpleNamespace(retrieve=boom))),
    )
    with caplog.at_level(logging.WARNING):
        assert creator_payouts.payout_status(uid) == "unavailable"
        assert creator_payouts.ready_account_id(uid) is None
    assert "stage='status_account_retrieve'" in caplog.text and "http_status=403" in caplog.text


def test_ready_account_id_only_when_stripe_says_ready(backend, monkeypatch):
    table, _, _, _ = backend
    uid = str(uuid4())
    assert creator_payouts.ready_account_id(uid) is None
    table.rows[uid] = "acct_ready"
    assert creator_payouts.ready_account_id(uid) == "acct_ready"
    monkeypatch.setattr(
        creator_payouts, "get_stripe_client",
        lambda: SimpleNamespace(v1=SimpleNamespace(accounts=SimpleNamespace(
            retrieve=lambda _id: {"payouts_enabled": True, "capabilities": {"transfers": "inactive"}}))),
    )
    assert creator_payouts.ready_account_id(uid) is None


def test_failed_create_is_not_replayed_for_24_hours(backend, monkeypatch):
    """Stripe caches a failed create under its idempotency key for 24h, so a
    fixed per-creator key kept failing after the cause was fixed. Same window
    -> same key (double-submit safe); next window -> a fresh attempt."""
    _, created, _, _ = backend
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


# --------------------------------------------------------------------------
# Production failure after the sk_test key fix (stage='account_create',
# HTTP 400 invalid_request_error): this platform's Stripe account no longer
# accepts `type=express` on POST /v1/accounts and requires the fee payer and
# loss liability (plus the account email for a transfers-only, i.e.
# "recipient", account) to be set at creation. The stub below enforces that
# rule with Stripe's exact message, through the REAL SDK.
# --------------------------------------------------------------------------

PRODUCTION_MESSAGE = (
    "Stripe no longer recommends Accounts v1 for new Connect integrations. Create connected "
    "accounts with POST /v2/core/accounts instead. If you are required to use Accounts v1, "
    "[set once at account creation]: an account email address for the account, required "
    "whenever a 'recipient' configuration is used, and controller[fees][payer] and "
    "controller[losses][payments]."
)


def _stripe_v1_account_rule(form: dict) -> tuple[int, dict]:
    if ("type" in form or "controller[fees][payer]" not in form
            or "controller[losses][payments]" not in form or "email" not in form):
        return 400, {"error": {"type": "invalid_request_error", "message": PRODUCTION_MESSAGE}}
    return 200, {"id": "acct_ruleaccepted", "object": "account"}


_LINK_OK = (200, {"object": "account_link", "url": "https://connect.stripe.com/setup/e/acct_ruleaccepted/x"})


def test_stub_reproduces_the_production_rejection_of_type_express(backend, real_sdk):
    """The rule stub rejects exactly what production rejected (the 11c8a22
    request shape), so the next test proves the fix against that rule."""
    stub = real_sdk({"/v1/accounts": _stripe_v1_account_rule, "/v1/account_links": _LINK_OK})
    client = stub.client()
    with pytest.raises(stripe.InvalidRequestError) as exc:
        client.v1.accounts.create({"type": "express", "capabilities": {"transfers": {"requested": True}}})
    assert exc.value.http_status == 400 and exc.value.code is None
    assert exc.value.user_message == PRODUCTION_MESSAGE
    assert getattr(exc.value.error, "type", None) == "invalid_request_error"


def test_onboarding_passes_the_production_account_rule(backend, real_sdk, caplog):
    table = backend[0]
    stub = real_sdk({"/v1/accounts": _stripe_v1_account_rule, "/v1/account_links": _LINK_OK})
    uid = str(uuid4())
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"):
        url = creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert url == "https://connect.stripe.com/setup/e/acct_ruleaccepted/x"
    assert table.rows == {uid: "acct_ruleaccepted"}
    assert [r["path"] for r in stub.requests] == ["/v1/accounts", "/v1/account_links"]
    assert "type" not in stub.requests[0]["form"]
    assert not [r for r in caplog.records if r.name == "app.services.creator_payouts"]


def test_production_style_rejection_is_still_logged_and_failed_closed(backend, real_sdk, caplog):
    real_sdk({"/v1/accounts": (400, {"error": {"type": "invalid_request_error",
                                               "message": PRODUCTION_MESSAGE}}),
              "/v1/account_links": _LINK_OK})
    with caplog.at_level(logging.WARNING, logger="app.services.creator_payouts"), \
            pytest.raises(creator_payouts.PayoutSetupError, match="^Payout setup is unavailable$"):
        creator_payouts.onboarding_url(str(uuid4()), create_if_missing=True)
    line = caplog.records[-1].getMessage()
    assert "stage='account_create'" in line and "key_kind=" in line and "http_status=400" in line
    assert "stripe_code=None" in line and "stripe_type='invalid_request_error'" in line
    assert "controller[fees][payer]" in line and CONTRACT_KEY not in line
    assert backend[0].rows == {}


def test_email_is_read_from_the_creator_row_and_masked_in_logs(backend, real_sdk, caplog):
    table = backend[0]
    uid = str(uuid4())
    table.users.emails[uid] = "maya.lee@example.com"
    real_sdk({"/v1/accounts": (400, {"error": {"type": "invalid_request_error",
                                               "message": "Invalid email address: maya.lee@example.com"}}),
              "/v1/account_links": _LINK_OK})
    with caplog.at_level(logging.WARNING), pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert table.users.reads == [uid]
    assert "maya.lee@example.com" not in caplog.text and "[email]" in caplog.text


@pytest.mark.parametrize("email", [None, "", "not-an-email"])
def test_missing_email_fails_closed_before_any_stripe_call(backend, email, caplog):
    table, created, _, _ = backend
    uid = str(uuid4())
    table.users.emails[uid] = email
    with caplog.at_level(logging.WARNING), pytest.raises(creator_payouts.PayoutSetupError):
        creator_payouts.onboarding_url(uid, create_if_missing=True)
    assert created == [] and table.rows == {}
    assert "stage='account_email'" in caplog.text


def test_existing_accounts_and_refresh_never_read_email_or_create(backend):
    table, created, links, _ = backend
    uid = str(uuid4())
    table.rows[uid] = "acct_existing"
    creator_payouts.onboarding_url(uid, create_if_missing=True)
    creator_payouts.onboarding_url(uid, create_if_missing=False)
    assert created == [] and table.users.reads == []
    assert [link["account"] for link in links] == ["acct_existing", "acct_existing"]


def test_long_stripe_messages_are_logged_whole():
    """Production's ~360-char message was cut at 300 chars, hiding the
    required field names. The whole message (masked) must reach the log."""
    from app.services import stripe_client

    fields = stripe_client.safe_error_fields(RuntimeError(PRODUCTION_MESSAGE))
    assert fields["message"] == PRODUCTION_MESSAGE
    assert stripe_client.safe_error_fields(RuntimeError("x" * 5000))["message"] == "x" * 1000
