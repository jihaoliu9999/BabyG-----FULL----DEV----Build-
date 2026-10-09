"""Permanent account deletion (migration 0053 + POST /creator/profile/delete).

The database side — the ten NO ACTION foreign keys and the atomic
`delete_user_account` RPC — is exercised against real Postgres outside
pytest. These tests pin the application contract around it:

  * the Google grant is read before deletion and revoked only after the
    database confirms "deleted";
  * "blocked", "not_found" and every failure never revoke anything;
  * the session is cleared only when the account is gone;
  * the route stays refused unless BABYG_ACCOUNT_DELETION_ENABLED is on.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from postgrest.exceptions import APIError as PostgrestAPIError

from app.core.security import PENDING_ROLE_COOKIE, SESSION_COOKIE, write_session
from app.integrations import google_calendar
from app.main import app
from app.routes import creator as creator_routes
from app.services import profiles

USER_ID = "creator-1"


class _Query:
    def __init__(self, fake: _FakeSupabase, table: str) -> None:
        self.fake = fake
        self.table = table
        self.filters: list[tuple[str, Any]] = []

    def select(self, columns: str) -> _Query:
        self.columns = columns
        return self

    def eq(self, column: str, value: Any) -> _Query:
        self.filters.append((column, value))
        return self

    def limit(self, _n: int) -> _Query:
        return self

    def execute(self) -> SimpleNamespace:
        self.fake.calls.append(("select", self.table, tuple(self.filters)))
        if self.fake.lookup_error is not None:
            raise self.fake.lookup_error
        return SimpleNamespace(data=self.fake.google_rows)


class _Rpc:
    def __init__(self, fake: _FakeSupabase, name: str, params: dict[str, Any]) -> None:
        self.fake = fake
        self.name = name
        self.params = params

    def execute(self) -> SimpleNamespace:
        self.fake.calls.append(("rpc", self.name, self.params))
        if self.fake.rpc_error is not None:
            raise self.fake.rpc_error
        return SimpleNamespace(data=self.fake.rpc_data)


class _FakeSupabase:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.google_rows: list[dict[str, Any]] = []
        self.lookup_error: Exception | None = None
        self.rpc_data: Any = [{"result": "deleted"}]
        self.rpc_error: Exception | None = None

    def table(self, name: str) -> _Query:
        return _Query(self, name)

    def rpc(self, name: str, params: dict[str, Any]) -> _Rpc:
        return _Rpc(self, name, params)


@pytest.fixture()
def fake_db(monkeypatch) -> _FakeSupabase:
    fake = _FakeSupabase()
    monkeypatch.setattr(profiles.supabase_client, "get_service_client", lambda: fake)
    return fake


@pytest.fixture()
def revokes(monkeypatch, fake_db: _FakeSupabase) -> list[str]:
    seen: list[str] = []

    def _revoke(token: str) -> bool:
        fake_db.calls.append(("revoke",))
        seen.append(token)
        return True

    monkeypatch.setattr(profiles.google_calendar, "revoke_token", _revoke)
    return seen


def _api_error() -> PostgrestAPIError:
    return PostgrestAPIError({"message": "boom", "code": "XX000"})


# ---------------------------------------------------------------- service


def test_deleted_revokes_google_only_after_the_database_confirms(fake_db, revokes) -> None:
    fake_db.google_rows = [{"access_token": "at-1", "refresh_token": "rt-1"}]

    outcome = profiles.delete_account(USER_ID)

    assert outcome == profiles.AccountDeletion("deleted", google_revoked=True)
    assert [call[0] for call in fake_db.calls] == ["select", "rpc", "revoke"]
    assert fake_db.calls[0] == (
        "select",
        "oauth_connections",
        (("user_id", USER_ID), ("provider", "google")),
    )
    assert fake_db.calls[1] == ("rpc", "delete_user_account", {"p_user_id": USER_ID})
    # The refresh token revokes the whole grant tree.
    assert revokes == ["rt-1"]


def test_access_token_is_revoked_when_no_refresh_token(fake_db, revokes) -> None:
    fake_db.google_rows = [{"access_token": "at-1", "refresh_token": None}]

    assert profiles.delete_account(USER_ID).google_revoked is True
    assert revokes == ["at-1"]


def test_no_google_connection_means_nothing_to_revoke(fake_db, revokes) -> None:
    outcome = profiles.delete_account(USER_ID)

    assert outcome == profiles.AccountDeletion("deleted", google_revoked=None)
    assert revokes == []


def test_google_revoke_failure_is_reported_without_undoing_the_deletion(
    monkeypatch, fake_db
) -> None:
    fake_db.google_rows = [{"access_token": "at-1", "refresh_token": "rt-1"}]

    def _fail(_token: str) -> bool:
        raise google_calendar.GoogleCalendarError("Google OAuth token revoke failed")

    monkeypatch.setattr(profiles.google_calendar, "revoke_token", _fail)

    assert profiles.delete_account(USER_ID) == profiles.AccountDeletion(
        "deleted", google_revoked=False
    )


@pytest.mark.parametrize("data", [["deleted"], [{"result": "deleted"}]])
def test_both_postgrest_row_shapes_are_understood(fake_db, revokes, data) -> None:
    fake_db.rpc_data = data

    assert profiles.delete_account(USER_ID).status == "deleted"


@pytest.mark.parametrize("status", ["blocked", "not_found"])
def test_refusal_and_missing_account_never_revoke(fake_db, revokes, status) -> None:
    fake_db.google_rows = [{"access_token": "at-1", "refresh_token": "rt-1"}]
    fake_db.rpc_data = [{"result": status}]

    assert profiles.delete_account(USER_ID) == profiles.AccountDeletion(status)
    assert revokes == []


@pytest.mark.parametrize(
    "error",
    [_api_error(), httpx.ConnectError("down"), httpx.ReadTimeout("slow")],
    ids=["postgrest-error", "connect-error", "timeout"],
)
def test_rpc_failure_is_failed_and_never_revokes(fake_db, revokes, error) -> None:
    fake_db.google_rows = [{"access_token": "at-1", "refresh_token": "rt-1"}]
    fake_db.rpc_error = error

    assert profiles.delete_account(USER_ID) == profiles.AccountDeletion("failed")
    assert revokes == []


@pytest.mark.parametrize(
    "data",
    [[], ["deleted", "deleted"], ["gone"], [{"status": "deleted"}], "deleted", None],
    ids=["empty", "two-rows", "unknown-value", "wrong-key", "scalar", "none"],
)
def test_unrecognised_rpc_result_is_failed_not_success(fake_db, revokes, data) -> None:
    fake_db.google_rows = [{"access_token": "at-1", "refresh_token": "rt-1"}]
    fake_db.rpc_data = data

    assert profiles.delete_account(USER_ID) == profiles.AccountDeletion("failed")
    assert revokes == []


@pytest.mark.parametrize(
    "error", [_api_error(), httpx.ConnectError("down")], ids=["postgrest-error", "connect-error"]
)
def test_grant_lookup_failure_stops_before_deleting_anything(fake_db, revokes, error) -> None:
    fake_db.lookup_error = error

    assert profiles.delete_account(USER_ID) == profiles.AccountDeletion("failed")
    assert [call[0] for call in fake_db.calls] == ["select"]
    assert revokes == []


def test_missing_supabase_env_is_not_disguised_as_a_result(monkeypatch) -> None:
    def _missing() -> None:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set")

    monkeypatch.setattr(profiles.supabase_client, "get_service_client", _missing)

    with pytest.raises(RuntimeError):
        profiles.delete_account(USER_ID)


# ------------------------------------------------------------------ route


def _signed_in(client: TestClient, *, role: str = "creator", user_id: str = USER_ID) -> str:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return cookie


@pytest.fixture()
def enabled(monkeypatch) -> None:
    monkeypatch.setenv("BABYG_ACCOUNT_DELETION_ENABLED", "true")
    creator_routes.get_settings.cache_clear()


@pytest.fixture()
def outcomes(monkeypatch) -> SimpleNamespace:
    """Queued delete_account results, plus the user ids it was called with."""
    stub = SimpleNamespace(queue=[], calls=[])

    def _delete(user_id: str) -> profiles.AccountDeletion:
        stub.calls.append(user_id)
        result = stub.queue.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(creator_routes.profiles, "delete_account", _delete)
    return stub


def _post(client: TestClient, data: dict[str, str] | None = None) -> Any:
    return client.post(
        "/creator/profile/delete",
        data={"confirm": "delete"} if data is None else data,
        follow_redirects=False,
    )


def _cleared(response: Any, name: str) -> bool:
    return any(
        header.startswith(f"{name}=") and ("Max-Age=0" in header or "expires=" in header.lower())
        for header in response.headers.get_list("set-cookie")
    )


def test_flag_off_keeps_the_temporary_refusal(monkeypatch, client: TestClient, outcomes) -> None:
    monkeypatch.delenv("BABYG_ACCOUNT_DELETION_ENABLED", raising=False)
    creator_routes.get_settings.cache_clear()
    _signed_in(client)

    response = _post(client)

    assert response.headers["location"] == (
        "/creator/profile/settings?delete=unavailable#delete-account"
    )
    assert "set-cookie" not in response.headers
    assert outcomes.calls == []


@pytest.mark.parametrize("confirm", ["", "nope", "delete it", "deleted"])
def test_confirmation_word_is_required(enabled, client: TestClient, outcomes, confirm) -> None:
    session_cookie = _signed_in(client)

    response = _post(client, {"confirm": confirm} if confirm else {})

    assert response.status_code == 303
    assert response.headers["location"] == (
        "/creator/profile/settings?delete=confirm#delete-account"
    )
    assert "set-cookie" not in response.headers
    assert client.cookies.get(SESSION_COOKIE) == session_cookie
    assert outcomes.calls == []


@pytest.mark.parametrize("confirm", ["delete", "DELETE", "  Delete  "])
def test_deleted_clears_session_and_lands_on_confirmation(
    enabled, client: TestClient, outcomes, confirm
) -> None:
    _signed_in(client)
    outcomes.queue.append(profiles.AccountDeletion("deleted", google_revoked=True))

    response = _post(client, {"confirm": confirm, "user_id": "someone-else"})

    assert response.status_code == 303
    assert response.headers["location"] == "/data-deletion?deleted=1#account-deleted"
    assert _cleared(response, SESSION_COOKIE)
    assert _cleared(response, PENDING_ROLE_COOKIE)
    # Only the signed session decides whose account is deleted.
    assert outcomes.calls == [USER_ID]


def test_google_revoke_failure_is_surfaced_after_deletion(
    enabled, client: TestClient, outcomes
) -> None:
    _signed_in(client)
    outcomes.queue.append(profiles.AccountDeletion("deleted", google_revoked=False))

    response = _post(client)

    assert response.headers["location"] == (
        "/data-deletion?deleted=1&google=revoke_failed#account-deleted"
    )
    assert _cleared(response, SESSION_COOKIE)


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_refusal_and_failure_preserve_the_session(
    enabled, monkeypatch, client: TestClient, outcomes, status
) -> None:
    session_cookie = _signed_in(client)
    outcomes.queue.append(profiles.AccountDeletion(status))

    response = _post(client)

    assert response.status_code == 303
    assert response.headers["location"] == (
        f"/creator/profile/settings?delete={status}#delete-account"
    )
    assert "set-cookie" not in response.headers
    assert client.cookies.get(SESSION_COOKIE) == session_cookie
    assert "deleted=" not in response.headers["location"]


def test_unexpected_error_does_not_clear_the_session(
    enabled, monkeypatch, outcomes
) -> None:
    client = TestClient(app, raise_server_exceptions=False)
    _signed_in(client)
    outcomes.queue.append(RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set"))

    response = _post(client)

    assert response.status_code == 500
    assert not _cleared(response, SESSION_COOKIE)


def test_missing_account_signs_out_without_claiming_this_request_deleted_it(
    enabled, client: TestClient, outcomes
) -> None:
    _signed_in(client)
    outcomes.queue.append(profiles.AccountDeletion("not_found"))

    response = _post(client)

    assert response.headers["location"] == "/data-deletion?deleted=gone#account-deleted"
    assert _cleared(response, SESSION_COOKIE)


def test_duplicate_requests_report_one_deletion(enabled, client: TestClient, outcomes) -> None:
    cookie = _signed_in(client)
    outcomes.queue.extend(
        [profiles.AccountDeletion("deleted"), profiles.AccountDeletion("not_found")]
    )

    first = _post(client)
    client.cookies.set(SESSION_COOKIE, cookie)  # a second tab still holds the cookie
    second = _post(client)

    assert first.headers["location"] == "/data-deletion?deleted=1#account-deleted"
    assert second.headers["location"] == "/data-deletion?deleted=gone#account-deleted"
    assert outcomes.calls == [USER_ID, USER_ID]


@pytest.mark.parametrize("role, expected_status", [(None, 401), ("brand", 403), ("operator", 403)])
def test_requires_a_creator_session_when_enabled(
    enabled, client: TestClient, outcomes, role, expected_status
) -> None:
    if role:
        _signed_in(client, role=role)

    response = _post(client)

    assert response.status_code == expected_status
    assert outcomes.calls == []


def test_get_is_not_a_deletion_path(enabled, client: TestClient, outcomes) -> None:
    _signed_in(client)

    response = client.get("/creator/profile/delete", follow_redirects=False)

    assert response.status_code == 405
    assert outcomes.calls == []


# -------------------------------------------------------------- templates


def _settings_page(monkeypatch, client: TestClient, query: str = "") -> str:
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda _uid: {"onboarding_completed_at": "2026-05-01T00:00:00Z", "full_name": "Mia"},
    )
    monkeypatch.setattr(creator_routes.oauth_connections, "get_google_connection", lambda _uid: None)
    monkeypatch.setattr(creator_routes.google_calendar, "is_configured", lambda: False)
    response = client.get(f"/creator/profile/settings{query}")
    assert response.status_code == 200
    return response.text


def test_settings_shows_the_deletion_form_only_when_enabled(
    enabled, monkeypatch, client: TestClient
) -> None:
    html = _settings_page(monkeypatch, client)

    assert 'action="/creator/profile/delete"' in html
    assert 'name="confirm"' in html
    assert 'pattern="[Dd][Ee][Ll][Ee][Tt][Ee]"' in html
    assert 'href="https://accounts.meta.com/apps/"' in html
    assert "Account deletion is temporarily unavailable" not in html


@pytest.mark.parametrize(
    "reason, copy",
    [
        ("confirm", "in the box below to confirm."),
        ("blocked", "applications, offers or deals with other people are tied to it. nothing was deleted."),
        ("failed", "we couldn't confirm your account was deleted. you're still signed in"),
    ],
)
def test_settings_explains_each_outcome(
    enabled, monkeypatch, client: TestClient, reason, copy
) -> None:
    html = _settings_page(monkeypatch, client, f"?delete={reason}")

    assert copy in html
    assert 'id="delete-account" open>' in html


def test_data_deletion_page_describes_in_app_flow_only_when_enabled(
    enabled, client: TestClient
) -> None:
    html = client.get("/data-deletion").text

    assert "delete your account inside babyg" in html
    assert "in-app deletion is refused and nothing is deleted" in html
    assert "temporarily unavailable" not in html
    assert 'id="account-deleted"' not in html


def test_data_deletion_confirmation_copy(client: TestClient) -> None:
    deleted = client.get("/data-deletion?deleted=1").text
    assert "your babyg account has been deleted and you've been signed out." in deleted
    assert 'href="https://accounts.meta.com/apps/"' in deleted
    assert "myaccount.google.com/permissions" not in deleted

    revoke_failed = client.get("/data-deletion?deleted=1&google=revoke_failed").text
    assert "google didn't confirm that babyg's access was revoked" in revoke_failed
    assert 'href="https://myaccount.google.com/permissions"' in revoke_failed

    gone = client.get("/data-deletion?deleted=gone").text
    assert "this babyg account no longer exists, so you've been signed out." in gone
    assert "has been deleted and" not in gone

    plain = client.get("/data-deletion").text
    assert 'id="account-deleted"' not in plain
