"""Sandbox Connect onboarding and creator-only Settings contract."""

from __future__ import annotations

from types import SimpleNamespace
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


@pytest.fixture()
def backend(monkeypatch):
    table = FakeTable()
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
        lambda: SimpleNamespace(table=lambda name: table if name == creator_payouts.TABLE else None),
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
    assert created[0][0] == {"type": "express", "capabilities": {"transfers": {"requested": True}}}
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
