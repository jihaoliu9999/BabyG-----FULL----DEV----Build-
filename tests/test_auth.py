"""Auth flow tests.

We stub both Supabase clients so the suite never makes a real network call.
Three layers covered:

  * Public marketing pages render and link into the auth flow.
  * /auth/magic-link initiates the OTP and stashes the pending-role cookie.
  * /auth/callback verifies the OTP, ensures the public.users row, writes
    the session cookie, and redirects role-correctly.
  * /auth/logout clears the cookie.
  * require_role() denies unmatched roles.

We don't unit-test Supabase itself; we just assert that our code calls it
with the right arguments and does the right thing with the result.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core import supabase_client
from app.core.security import (
    PENDING_ROLE_COOKIE,
    SESSION_COOKIE,
    write_session,
)
from app.main import app

# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


class FakeAuth:
    def __init__(self) -> None:
        self.last_otp_args: dict[str, Any] | None = None
        self.last_verify_args: dict[str, Any] | None = None
        self.last_set_session_args: tuple[str, str] | None = None
        self.last_code_exchange_args: dict[str, Any] | None = None
        self.verify_response: Any = None
        self.set_session_response: Any = None
        self.code_exchange_response: Any = None
        self.verify_should_raise: Exception | None = None
        self.set_session_should_raise: Exception | None = None
        self.code_exchange_should_raise: Exception | None = None
        self.otp_should_raise: Exception | None = None

    def sign_in_with_otp(self, args: dict[str, Any]) -> Any:
        self.last_otp_args = args
        if self.otp_should_raise:
            raise self.otp_should_raise
        return SimpleNamespace()

    def verify_otp(self, args: dict[str, Any]) -> Any:
        self.last_verify_args = args
        if self.verify_should_raise:
            raise self.verify_should_raise
        return self.verify_response

    def set_session(self, access_token: str, refresh_token: str) -> Any:
        self.last_set_session_args = (access_token, refresh_token)
        if self.set_session_should_raise:
            raise self.set_session_should_raise
        return self.set_session_response

    def exchange_code_for_session(self, args: dict[str, Any]) -> Any:
        self.last_code_exchange_args = args
        if self.code_exchange_should_raise:
            raise self.code_exchange_should_raise
        return self.code_exchange_response


class FakeTable:
    """Records calls and returns canned data for one-table chains."""

    def __init__(self, name: str, store: dict[str, list[dict]]) -> None:
        self.name = name
        self.store = store
        self._filters: list[tuple[str, Any]] = []

    def select(self, *_args, **_kwargs) -> FakeTable:
        return self

    def eq(self, col: str, val: Any) -> FakeTable:
        self._filters.append((col, val))
        return self

    def limit(self, _n: int) -> FakeTable:
        return self

    def execute(self) -> Any:
        rows = self.store.get(self.name, [])
        for col, val in self._filters:
            rows = [r for r in rows if r.get(col) == val]
        return SimpleNamespace(data=rows)

    def insert(self, payload: dict) -> FakeTable:
        self._pending_insert = payload
        return self

    # Allow execute() after insert by overloading via attribute.
    def __getattr__(self, item):
        raise AttributeError(item)


class FakeService:
    def __init__(self) -> None:
        self.tables: dict[str, list[dict]] = {
            "users": [],
            "creator_profiles": [],
            "brand_profiles": [],
        }
        self.inserts: list[tuple[str, dict]] = []

    def table(self, name: str) -> FakeServiceTable:
        return FakeServiceTable(name, self)


class FakeServiceTable:
    def __init__(self, name: str, parent: FakeService) -> None:
        self.name = name
        self.parent = parent
        self._filters: list[tuple[str, Any]] = []
        self._pending_insert: dict | None = None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self._filters.append((col, val))
        return self

    def limit(self, _n):
        return self

    def insert(self, payload):
        self._pending_insert = payload
        return self

    def upsert(self, payload, *, on_conflict=None, ignore_duplicates=False):
        # Treat the conflict column as the natural key. If a matching row
        # exists, return it (or skip when ignore_duplicates) instead of
        # inserting; otherwise behave like insert.
        rows = self.parent.tables.setdefault(self.name, [])
        key_col = (on_conflict or "").split(",")[0].strip() if on_conflict else None
        if key_col:
            existing = next(
                (r for r in rows if r.get(key_col) == payload.get(key_col)), None
            )
            if existing is not None:
                self._pending_insert = None
                self._upsert_existing = existing
                return self
        self._pending_insert = payload
        return self

    def execute(self):
        if self._pending_insert is not None:
            self.parent.inserts.append((self.name, self._pending_insert))
            self.parent.tables.setdefault(self.name, []).append(self._pending_insert)
            return SimpleNamespace(data=[self._pending_insert])
        rows = self.parent.tables.get(self.name, [])
        for col, val in self._filters:
            rows = [r for r in rows if r.get(col) == val]
        return SimpleNamespace(data=list(rows))


@pytest.fixture()
def fake_auth(monkeypatch) -> FakeAuth:
    auth = FakeAuth()
    fake_anon = SimpleNamespace(auth=auth)
    monkeypatch.setattr(supabase_client, "get_anon_client", lambda: fake_anon)
    # Bypass the lru_cache so route imports get the new function each test.
    return auth


@pytest.fixture()
def fake_service(monkeypatch) -> FakeService:
    svc = FakeService()
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: svc)
    return svc


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


# -----------------------------------------------------------------------------
# Marketing pages
# -----------------------------------------------------------------------------


def test_landing_renders(client: TestClient) -> None:
    r = client.get("/")
    assert r.status_code == 200
    assert "get access" in r.text


def test_get_started_renders_role_cards(client: TestClient) -> None:
    r = client.get("/get-started")
    assert r.status_code == 200
    assert "/auth/login?role=creator" in r.text
    assert "/auth/login?role=brand" in r.text
    # Operator card removed from the visual chooser — see
    # tests/test_marketing.py::test_get_started_renders_role_cards for the
    # full rationale. Operators sign in via a direct /auth/login?role=operator
    # URL from an admin invite; the route still accepts it.
    assert "/auth/login?role=operator" not in r.text


def test_get_started_with_role_query_redirects(client: TestClient) -> None:
    r = client.get("/get-started?role=creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?role=creator"


def test_get_started_with_brand_role_query_redirects(client: TestClient) -> None:
    r = client.get("/get-started?role=brand")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?role=brand"


def test_landing_redirects_signed_in_users(client: TestClient) -> None:
    # Sign a session cookie directly (bypasses the magic-link flow).
    from fastapi import Response as _R

    resp = _R()
    write_session(resp, {"user_id": "u1", "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)

    r = client.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/creator"


# -----------------------------------------------------------------------------
# /auth/login form
# -----------------------------------------------------------------------------


def test_login_form_renders(client: TestClient) -> None:
    r = client.get("/auth/login?role=creator")
    assert r.status_code == 200
    assert "creator sign in" in r.text


def test_login_form_renders_brand(client: TestClient) -> None:
    r = client.get("/auth/login?role=brand")
    assert r.status_code == 200
    assert "brand sign in" in r.text


def test_login_form_falls_back_to_creator_for_unknown_role(client: TestClient) -> None:
    r = client.get("/auth/login?role=ufo")
    assert r.status_code == 200
    assert "creator sign in" in r.text


# -----------------------------------------------------------------------------
# /auth/magic-link
# -----------------------------------------------------------------------------


def test_magic_link_sends_otp_and_sets_pending_role(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.post(
        "/auth/magic-link",
        data={"email": "Anna@Example.com", "role": "creator"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/magic-link/sent?role=creator"

    assert fake_auth.last_otp_args is not None
    assert fake_auth.last_otp_args["email"] == "anna@example.com"
    assert fake_auth.last_otp_args["options"]["email_redirect_to"] == (
        "http://localhost:8000/auth/callback"
    )
    assert (
        fake_auth.last_otp_args["options"]["data"]["requested_role"] == "creator"
    )

    cookies = {c.name: c.value for c in client.cookies.jar}
    assert PENDING_ROLE_COOKIE in cookies


def test_magic_link_sends_brand_otp_and_sets_pending_role(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.post(
        "/auth/magic-link",
        data={"email": "Brand@Example.com", "role": "brand"},
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/magic-link/sent?role=brand"
    assert fake_auth.last_otp_args is not None
    assert fake_auth.last_otp_args["email"] == "brand@example.com"
    assert fake_auth.last_otp_args["options"]["data"]["requested_role"] == "brand"

    cookies = {c.name: c.value for c in client.cookies.jar}
    assert PENDING_ROLE_COOKIE in cookies


def test_magic_link_prefers_canonical_public_app_url(
    monkeypatch,
    client: TestClient,
    fake_auth: FakeAuth,
    fake_service: FakeService,
) -> None:
    from app.routes import auth as auth_route

    monkeypatch.setenv("APP_URL", "https://babyg.ai")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://www.babyg.ai/")
    monkeypatch.setattr(
        auth_route.magic_link_limiter,
        "allow",
        lambda *_args, **_kwargs: True,
    )

    r = client.post(
        "/auth/magic-link",
        data={"email": "anna@example.com", "role": "creator"},
    )

    assert r.status_code == 303
    assert fake_auth.last_otp_args is not None
    assert fake_auth.last_otp_args["options"]["email_redirect_to"] == (
        "https://www.babyg.ai/auth/callback"
    )


def test_magic_link_rejects_invalid_email(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.post("/auth/magic-link", data={"email": "not-an-email", "role": "creator"})
    assert r.status_code == 400
    assert fake_auth.last_otp_args is None


def test_magic_link_operator_silently_skips_send_for_unknown_email(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    # No operator row exists yet → magic-link should NOT call sign_in_with_otp.
    r = client.post(
        "/auth/magic-link", data={"email": "ghost@example.com", "role": "operator"}
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/auth/magic-link/sent?role=operator"
    assert fake_auth.last_otp_args is None


def test_magic_link_operator_sends_for_known_operator(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_service.tables["users"].append(
        {"id": "op-1", "email": "op@example.com", "role": "operator"}
    )
    r = client.post(
        "/auth/magic-link", data={"email": "op@example.com", "role": "operator"}
    )
    assert r.status_code == 303
    assert fake_auth.last_otp_args is not None
    assert fake_auth.last_otp_args["email"] == "op@example.com"


# -----------------------------------------------------------------------------
# /auth/callback
# -----------------------------------------------------------------------------


def _set_pending_role(client: TestClient, role: str) -> None:
    from fastapi import Response as _R

    from app.core.security import write_pending_role

    resp = _R()
    write_pending_role(resp, role)
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(PENDING_ROLE_COOKIE, cookie)


def test_callback_creates_creator_row_and_redirects(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="user-123", email="new@example.com")
    )
    _set_pending_role(client, "creator")

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"
    assert fake_auth.last_verify_args == {"token_hash": "abc", "type": "magiclink"}

    inserted_tables = [name for name, _ in fake_service.inserts]
    assert "users" in inserted_tables
    assert "creator_profiles" in inserted_tables
    users_insert = next(p for n, p in fake_service.inserts if n == "users")
    assert users_insert == {
        "id": "user-123",
        "email": "new@example.com",
        "role": "creator",
    }

    cookies = {c.name: c.value for c in client.cookies.jar}
    assert SESSION_COOKIE in cookies


def test_callback_creates_creator_from_metadata_without_pending_cookie(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(
            id="cross-browser-user",
            email="cross-browser@example.com",
            user_metadata={"requested_role": "creator"},
        )
    )

    r = client.get("/auth/callback?token_hash=abc&type=email")

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"
    users_insert = next(p for n, p in fake_service.inserts if n == "users")
    assert users_insert == {
        "id": "cross-browser-user",
        "email": "cross-browser@example.com",
        "role": "creator",
    }


def test_callback_creates_brand_row_and_redirects(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="brand-123", email="brand@example.com")
    )
    _set_pending_role(client, "brand")

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 302
    assert r.headers["location"] == "/brand"
    inserted_tables = [name for name, _ in fake_service.inserts]
    assert "users" in inserted_tables
    assert "brand_profiles" in inserted_tables
    users_insert = next(p for n, p in fake_service.inserts if n == "users")
    assert users_insert == {
        "id": "brand-123",
        "email": "brand@example.com",
        "role": "brand",
    }
    brand_insert = next(p for n, p in fake_service.inserts if n == "brand_profiles")
    assert brand_insert == {
        "user_id": "brand-123",
        "company_name": "",
        "contact_full_name": "",
    }


def test_callback_creates_brand_from_metadata_without_pending_cookie(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(
            id="cross-browser-brand",
            email="brand-cross@example.com",
            user_metadata={"requested_role": "brand"},
        )
    )

    r = client.get("/auth/callback?token_hash=abc&type=email")

    assert r.status_code == 302
    assert r.headers["location"] == "/brand"
    users_insert = next(p for n, p in fake_service.inserts if n == "users")
    assert users_insert == {
        "id": "cross-browser-brand",
        "email": "brand-cross@example.com",
        "role": "brand",
    }


def test_callback_does_not_trust_operator_role_from_metadata(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(
            id="untrusted-operator",
            email="untrusted@example.com",
            user_metadata={"requested_role": "operator"},
        )
    )

    r = client.get("/auth/callback?token_hash=abc&type=email")

    assert r.status_code == 400
    assert fake_service.inserts == []


def test_callback_accepts_confirmation_url_fragment_bridge_tokens(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.set_session_response = SimpleNamespace(
        user=SimpleNamespace(id="user-fragment", email="fragment@example.com")
    )
    _set_pending_role(client, "creator")

    r = client.post(
        "/auth/callback",
        data={
            "access_token": "access.jwt",
            "refresh_token": "refresh-token",
            "type": "magiclink",
        },
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"
    assert fake_auth.last_set_session_args == ("access.jwt", "refresh-token")
    users_insert = next(p for n, p in fake_service.inserts if n == "users")
    assert users_insert == {
        "id": "user-fragment",
        "email": "fragment@example.com",
        "role": "creator",
    }


def test_callback_accepts_code_exchange(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_service.tables["users"].append(
        {"id": "code-user", "email": "code@example.com", "role": "creator"}
    )
    fake_auth.code_exchange_response = SimpleNamespace(
        session=SimpleNamespace(
            user=SimpleNamespace(id="code-user", email="code@example.com")
        )
    )

    r = client.get("/auth/callback?code=auth-code")

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"
    assert fake_auth.last_code_exchange_args == {"auth_code": "auth-code"}
    assert fake_service.inserts == []


def test_callback_existing_user_does_not_reinsert(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_service.tables["users"].append(
        {"id": "user-existing", "email": "ana@example.com", "role": "creator"}
    )
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="user-existing", email="ana@example.com")
    )
    _set_pending_role(client, "operator")  # cookie hint is ignored — existing role wins

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"  # existing role wins
    assert fake_service.inserts == []


def test_callback_refuses_first_time_operator(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="user-new-op", email="rando@example.com")
    )
    _set_pending_role(client, "operator")

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 400
    assert "operator" in r.text.lower()
    assert fake_service.inserts == []


def test_callback_with_bad_token_renders_error(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    from gotrue.errors import AuthApiError

    fake_auth.verify_should_raise = AuthApiError("invalid token", 400, None)

    r = client.get("/auth/callback?token_hash=bad&type=magiclink")

    assert r.status_code == 400
    assert "expired" in r.text.lower() or "invalid" in r.text.lower()


def test_callback_with_infra_failure_distinct_message(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    # Non-AuthApi exceptions get a distinct "try again later" message
    # so support can distinguish from real expired-link cases.
    fake_auth.verify_should_raise = RuntimeError("network down")
    r = client.get("/auth/callback?token_hash=abc&type=magiclink")
    assert r.status_code == 400
    assert "again" in r.text.lower()


def test_callback_with_unknown_otp_type_rejected(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    # Attacker-supplied `type` values are refused before we forward to Supabase.
    fake_auth.verify_response = SimpleNamespace()  # never read
    r = client.get("/auth/callback?token_hash=abc&type=bogus")
    assert r.status_code == 400


def test_callback_missing_params_renders_error(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.get("/auth/callback?callback_error=missing")
    assert r.status_code == 400
    assert "missing required parameters" in r.text.lower()


def test_callback_without_query_renders_fragment_bridge(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.get("/auth/callback")
    assert r.status_code == 200
    assert "/static/js/auth_callback.js" in r.text
    assert "finishing sign in" in r.text


# -----------------------------------------------------------------------------
# /auth/logout + require_role
# -----------------------------------------------------------------------------


def test_logout_clears_cookie(client: TestClient) -> None:
    from fastapi import Response as _R

    resp = _R()
    write_session(resp, {"user_id": "u", "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)

    r = client.post("/auth/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/"
    # Both cookies should be cleared so a re-sign-in within the
    # bg_pending_role expiry window can't resurrect the previous role
    # (AUDIT.md M9).
    set_cookie = r.headers.get("set-cookie", "")
    assert SESSION_COOKIE in set_cookie
    assert PENDING_ROLE_COOKIE in set_cookie


# -----------------------------------------------------------------------------
# Error-kind classification + recovery CTA
# -----------------------------------------------------------------------------


def test_callback_expired_renders_resend_link_cta(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    """Expired-link path surfaces the role-aware 'send me a new link' CTA so
    users can recover without reopening the role chooser."""
    from gotrue.errors import AuthApiError

    fake_auth.verify_should_raise = AuthApiError("expired", 400, None)
    _set_pending_role(client, "creator")

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 400
    assert "expired" in r.text.lower()
    assert "/auth/login?role=creator&hint=expired" in r.text


def test_callback_infra_failure_renders_try_again_cta(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    """Infra failures get a distinct 'our side hiccuped' panel so support can
    distinguish from real expired-link cases."""
    fake_auth.verify_should_raise = RuntimeError("network down")
    _set_pending_role(client, "creator")

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 400
    assert "hiccup" in r.text.lower()
    # CTA points back to /auth/login (no hint=expired) — fresh attempt.
    assert "/auth/login?role=creator" in r.text


def test_callback_role_mismatch_routes_to_role_chooser(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    """An unauthorized operator signup lands on the role-mismatch page with
    a CTA back to /get-started — not /auth/login, because the issue is the
    role, not the link."""
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="user-new-op", email="rando@example.com")
    )
    _set_pending_role(client, "operator")

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 400
    assert "role mismatch" in r.text.lower()
    assert "/get-started" in r.text


def test_callback_falls_back_to_creator_when_pending_cookie_missing(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    """No pending-role cookie (user pasted the link in a different browser)
    must not blow up; default the CTA to creator."""
    from gotrue.errors import AuthApiError

    fake_auth.verify_should_raise = AuthApiError("expired", 400, None)
    # No _set_pending_role call.

    r = client.get("/auth/callback?token_hash=abc&type=magiclink")

    assert r.status_code == 400
    assert "/auth/login?role=creator&hint=expired" in r.text


def test_login_hint_expired_shows_soft_banner(client: TestClient) -> None:
    r = client.get("/auth/login?role=creator&hint=expired")
    assert r.status_code == 200
    assert "your last link expired" in r.text.lower()


def test_login_hint_ignores_unknown_values(client: TestClient) -> None:
    """The hint param is closed-vocabulary; unknown values render nothing."""
    r = client.get("/auth/login?role=creator&hint=bogus")
    assert r.status_code == 200
    assert "your last link expired" not in r.text.lower()


# -----------------------------------------------------------------------------
# /auth/code fallback
# -----------------------------------------------------------------------------


def test_code_form_renders(client: TestClient) -> None:
    r = client.get("/auth/code?role=creator&email=anna@example.com")
    assert r.status_code == 200
    assert "enter the code" in r.text.lower()
    assert 'value="anna@example.com"' in r.text


def test_code_form_falls_back_to_creator_for_unknown_role(client: TestClient) -> None:
    r = client.get("/auth/code?role=ufo")
    assert r.status_code == 200
    assert 'value="creator"' in r.text


def test_code_submit_signs_in_existing_creator(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_service.tables["users"].append(
        {"id": "creator-1", "email": "anna@example.com", "role": "creator"}
    )
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="creator-1", email="anna@example.com")
    )

    r = client.post(
        "/auth/code",
        data={"email": "anna@example.com", "code": "123456", "role": "creator"},
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"
    assert fake_auth.last_verify_args == {
        "email": "anna@example.com",
        "token": "123456",
        "type": "email",
    }
    cookies = {c.name: c.value for c in client.cookies.jar}
    assert SESSION_COOKIE in cookies


def test_code_submit_creates_first_time_creator(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="new-creator", email="new@example.com")
    )

    r = client.post(
        "/auth/code",
        data={"email": "new@example.com", "code": "654321", "role": "creator"},
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/creator"
    users_insert = next(p for n, p in fake_service.inserts if n == "users")
    assert users_insert == {
        "id": "new-creator",
        "email": "new@example.com",
        "role": "creator",
    }


def test_code_submit_creates_first_time_brand(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="new-brand", email="brand@example.com")
    )

    r = client.post(
        "/auth/code",
        data={"email": "brand@example.com", "code": "654321", "role": "brand"},
    )

    assert r.status_code == 302
    assert r.headers["location"] == "/brand"
    inserted_tables = [name for name, _ in fake_service.inserts]
    assert "users" in inserted_tables
    assert "brand_profiles" in inserted_tables


def test_code_submit_strips_dashes_and_spaces(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    """Email clients sometimes line-break a 6-digit code as `123 456` or
    `123-456`. Accept both."""
    fake_service.tables["users"].append(
        {"id": "u1", "email": "a@b.com", "role": "creator"}
    )
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="u1", email="a@b.com")
    )

    r = client.post(
        "/auth/code",
        data={"email": "a@b.com", "code": "123 - 456", "role": "creator"},
    )

    assert r.status_code == 302
    assert fake_auth.last_verify_args is not None
    assert fake_auth.last_verify_args["token"] == "123456"


def test_code_submit_rejects_invalid_email(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.post(
        "/auth/code",
        data={"email": "not-an-email", "code": "123456", "role": "creator"},
    )
    assert r.status_code == 400
    assert fake_auth.last_verify_args is None


def test_code_submit_rejects_non_numeric_code(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.post(
        "/auth/code",
        data={"email": "a@b.com", "code": "abcdef", "role": "creator"},
    )
    assert r.status_code == 400
    assert fake_auth.last_verify_args is None


def test_code_submit_handles_auth_api_error(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    from gotrue.errors import AuthApiError

    fake_auth.verify_should_raise = AuthApiError("expired", 400, None)

    r = client.post(
        "/auth/code",
        data={"email": "a@b.com", "code": "123456", "role": "creator"},
    )

    assert r.status_code == 400
    assert "invalid or has expired" in r.text.lower()


def test_code_submit_handles_infra_failure(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    fake_auth.verify_should_raise = RuntimeError("supabase down")

    r = client.post(
        "/auth/code",
        data={"email": "a@b.com", "code": "123456", "role": "creator"},
    )

    assert r.status_code == 400
    # Apostrophe is HTML-escaped by Jinja; check the surrounding phrase.
    assert "verify the code right now" in r.text.lower()


def test_code_submit_refuses_first_time_operator(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    """First-time operator via code path is still rejected — operator is
    invite-only, same gate as the magic-link callback."""
    fake_auth.verify_response = SimpleNamespace(
        user=SimpleNamespace(id="new-op", email="rando@example.com")
    )

    r = client.post(
        "/auth/code",
        data={"email": "rando@example.com", "code": "123456", "role": "operator"},
    )

    assert r.status_code == 400
    assert "operator" in r.text.lower()
    assert fake_service.inserts == []


def test_magic_link_sent_page_links_to_code_fallback(
    client: TestClient, fake_auth: FakeAuth, fake_service: FakeService
) -> None:
    r = client.get("/auth/magic-link/sent?role=creator")
    assert r.status_code == 200
    assert "check your email" in r.text.lower()
    assert "got a code instead?" in r.text.lower()
    assert "/auth/code?role=creator" in r.text


def test_magic_link_sent_page_normalizes_unknown_role(client: TestClient) -> None:
    r = client.get("/auth/magic-link/sent?role=unknown")
    assert r.status_code == 200
    assert "creator sign in" in r.text.lower()


# -----------------------------------------------------------------------------
# require_role
# -----------------------------------------------------------------------------


def test_dashboard_requires_correct_role(
    client: TestClient, fake_service
) -> None:
    # Anonymous browser GET → redirect to login (was JSON 401 before).
    r = client.get("/creator")
    assert r.status_code == 302
    assert r.headers["location"] == "/auth/login?role=creator"

    # Anonymous non-HTML clients still get JSON 401.
    r_json = client.get("/creator", headers={"accept": "application/json"})
    assert r_json.status_code == 401

    # Operator session → 403 on /creator (require_role rejects).
    from fastapi import Response as _R

    resp = _R()
    write_session(resp, {"user_id": "u", "role": "operator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)

    r = client.get("/creator")
    assert r.status_code == 403
