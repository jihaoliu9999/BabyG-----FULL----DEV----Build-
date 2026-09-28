"""Google OAuth token refresh contract — never return the expired token.

Production incident (Sep 2026):
  * 4 of 5 Gmail-eligible creators were racking up
    ``GmailUnauthorizedError`` on every ``sweep_gmail_briefs`` tick.
  * Their stored access_tokens had expired in June/July.
  * Their refresh_tokens were still present but Google's refresh
    endpoint was rejecting them (unverified consent screen — refresh
    tokens age out after 7 days in that mode).
  * ``oauth_connections.access_token_for_google`` was catching the
    resulting ``GoogleCalendarError`` and returning
    ``access_token or None`` — i.e. handing the sweep the SAME
    known-expired credential.
  * The sweep then called Gmail with the dead token, Gmail 401'd,
    and the cycle repeated on the next tick, forever.

This suite locks the fix: every fall-through path in
``access_token_for_google`` returns ``None`` (never the expired
token) when a fresh token cannot be obtained. It also verifies
distinct handling for a permanent Google rejection (invalid_grant)
vs a transient network / 5xx failure — the latter can retry on the
next tick, the former needs the creator to reconnect. Per-creator
isolation in ``sweep_gmail_briefs`` is verified end-to-end.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from app.integrations import google_calendar
from app.services import bot_jobs, oauth_connections

# ---------------------------------------------------------------------------
# Fixture helpers.
# ---------------------------------------------------------------------------


def _future_iso(seconds: int = 3600) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: int = 3600) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def _stub_connection(monkeypatch: pytest.MonkeyPatch, connection: dict | None) -> None:
    monkeypatch.setattr(
        oauth_connections,
        "get_google_connection",
        lambda user_id: connection,
    )


def _stub_refresh(monkeypatch: pytest.MonkeyPatch, behavior: Any) -> list[str]:
    """Replace ``google_calendar.refresh_access_token``.

    Behavior can be:
      * a dict → returned as the refresh response
      * an Exception instance → raised
    Returns a list that gets appended-to on every call so tests can
    assert exactly how many refresh attempts happened.
    """
    calls: list[str] = []

    def _fake_refresh(refresh_token: str) -> dict:
        calls.append(refresh_token)
        if isinstance(behavior, BaseException):
            raise behavior
        return dict(behavior)

    monkeypatch.setattr(google_calendar, "refresh_access_token", _fake_refresh)
    return calls


def _stub_save(monkeypatch: pytest.MonkeyPatch, ok: bool = True) -> list[dict]:
    """Replace ``oauth_connections.save_google_connection`` and return a
    list of saved payloads so tests can inspect what got persisted."""
    saved: list[dict] = []

    def _fake_save(user_id: str, token_response: dict, *, requested_scopes=None) -> bool:
        saved.append({
            "user_id": user_id,
            "token_response": dict(token_response),
            "requested_scopes": list(requested_scopes) if requested_scopes else None,
        })
        return ok

    monkeypatch.setattr(oauth_connections, "save_google_connection", _fake_save)
    return saved


# ---------------------------------------------------------------------------
# 1. Valid access token → returned without any refresh attempt.
# ---------------------------------------------------------------------------


def test_valid_token_is_returned_without_refresh(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_connection(
        monkeypatch,
        {
            "access_token": "still-fresh",
            "refresh_token": "rt-xyz",
            "expires_at": _future_iso(),
        },
    )
    refresh_calls = _stub_refresh(monkeypatch, RuntimeError("must not refresh"))
    save_calls = _stub_save(monkeypatch, ok=True)

    token = oauth_connections.access_token_for_google("u-1")

    assert token == "still-fresh"
    assert refresh_calls == []  # refresh must NOT be attempted when the token is valid
    assert save_calls == []


# ---------------------------------------------------------------------------
# 2. Expired token + successful refresh → new token returned.
# ---------------------------------------------------------------------------


def test_expired_token_refreshes_and_returns_new(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_connection(
        monkeypatch,
        {
            "access_token": "stale-abc",
            "refresh_token": "rt-old",
            "expires_at": _past_iso(),
        },
    )
    refresh_calls = _stub_refresh(
        monkeypatch,
        {
            "access_token": "brand-new-token",
            "refresh_token": "rt-new",
            "expires_in": 3600,
            "scope": "https://www.googleapis.com/auth/gmail.readonly",
        },
    )
    _stub_save(monkeypatch, ok=True)

    token = oauth_connections.access_token_for_google("u-1")

    assert token == "brand-new-token"
    assert refresh_calls == ["rt-old"]


# ---------------------------------------------------------------------------
# 3. Successful refresh persists the new token + expiry.
# ---------------------------------------------------------------------------


def test_successful_refresh_persists_new_token_and_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_connection(
        monkeypatch,
        {
            "access_token": "stale",
            "refresh_token": "rt-old",
            "expires_at": _past_iso(),
        },
    )
    fresh_response = {
        "access_token": "fresh-2026",
        "refresh_token": "rt-new-from-google",
        "expires_in": 3600,
        "scope": "https://www.googleapis.com/auth/gmail.readonly",
    }
    _stub_refresh(monkeypatch, fresh_response)
    saved = _stub_save(monkeypatch, ok=True)

    oauth_connections.access_token_for_google("u-1")

    assert len(saved) == 1
    persisted = saved[0]
    assert persisted["user_id"] == "u-1"
    assert persisted["token_response"]["access_token"] == "fresh-2026"
    assert persisted["token_response"]["refresh_token"] == "rt-new-from-google"
    # save_google_connection is the one that translates expires_in into
    # an ISO expires_at (via _expires_at), so passing the raw response
    # is exactly what we need to lock.
    assert "expires_in" in persisted["token_response"]


# ---------------------------------------------------------------------------
# 4. Successful refresh without a new refresh_token → old refresh_token
#    preserved by save_google_connection.
# ---------------------------------------------------------------------------


def test_refresh_without_new_refresh_token_preserves_old(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The save_google_connection helper reads the existing row when
    the token_response omits refresh_token, and reuses the stored one.
    Verified here at the persistence layer, since that's where the
    preservation lives and where a future refactor could regress it."""
    existing_row = {
        "access_token": "stale",
        "refresh_token": "rt-still-here",
        "expires_at": _past_iso(),
        "scopes": ["https://www.googleapis.com/auth/gmail.readonly"],
    }
    monkeypatch.setattr(
        oauth_connections,
        "get_google_connection",
        lambda user_id: existing_row,
    )

    captured: list[dict] = []

    class _FakeSupabase:
        def table(self, name: str) -> _FakeSupabase:
            assert name == "oauth_connections"
            return self

        def upsert(self, payload: dict, *, on_conflict: str) -> _FakeSupabase:
            captured.append(dict(payload))
            return self

        def execute(self):
            return None

    monkeypatch.setattr(
        oauth_connections.supabase_client,
        "get_service_client",
        lambda: _FakeSupabase(),
    )

    ok = oauth_connections.save_google_connection(
        "u-1",
        {
            # Google returned no refresh_token — normal behavior on
            # subsequent refreshes.
            "access_token": "fresh-2026",
            "expires_in": 3600,
        },
    )
    assert ok is True
    assert len(captured) == 1
    assert captured[0]["refresh_token"] == "rt-still-here"
    # Scopes preserved too (no `scope` field in the response).
    assert captured[0]["scopes"] == ["https://www.googleapis.com/auth/gmail.readonly"]


# ---------------------------------------------------------------------------
# 5. THE FIX: expired token + failed refresh → None, NOT the expired token.
# ---------------------------------------------------------------------------


def test_expired_token_transient_refresh_failure_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Transient failure = generic GoogleCalendarError (network blip,
    5xx, malformed body). Before the fix the helper returned the
    stored (expired) access_token. After the fix it returns None."""
    _stub_connection(
        monkeypatch,
        {
            "access_token": "DEAD-DO-NOT-USE",
            "refresh_token": "rt-old",
            "expires_at": _past_iso(),
        },
    )
    _stub_refresh(monkeypatch, google_calendar.GoogleCalendarError("network blip"))
    save_calls = _stub_save(monkeypatch, ok=True)

    token = oauth_connections.access_token_for_google("u-1")

    assert token is None, (
        "regression: the helper handed the caller a known-expired "
        "access_token when refresh failed. Gmail will 401 on every use."
    )
    # We didn't successfully refresh, so nothing was persisted.
    assert save_calls == []


def test_expired_token_permanent_reject_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Permanent rejection = the refresh_token is unusable
    (invalid_grant, revoked, aged-out). The distinct
    GoogleTokenRefreshRejectedError signals ops that the creator
    needs to reconnect, but the return value is still None — we
    must NEVER hand back the expired access_token."""
    _stub_connection(
        monkeypatch,
        {
            "access_token": "DEAD-DO-NOT-USE",
            "refresh_token": "rt-revoked",
            "expires_at": _past_iso(),
        },
    )
    _stub_refresh(
        monkeypatch,
        google_calendar.GoogleTokenRefreshRejectedError("invalid_grant"),
    )
    save_calls = _stub_save(monkeypatch, ok=True)

    token = oauth_connections.access_token_for_google("u-1")

    assert token is None
    assert save_calls == []


def test_expired_token_no_refresh_token_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Expired access_token AND no refresh_token stored → return None.
    Before the fix this returned the expired access_token."""
    _stub_connection(
        monkeypatch,
        {
            "access_token": "DEAD",
            "refresh_token": "",
            "expires_at": _past_iso(),
        },
    )
    save_calls = _stub_save(monkeypatch, ok=True)
    token = oauth_connections.access_token_for_google("u-1")
    assert token is None
    assert save_calls == []


# ---------------------------------------------------------------------------
# 6. Per-creator isolation: one creator's refresh failure does NOT
#    stop sweep_gmail_briefs from processing other creators.
# ---------------------------------------------------------------------------


def test_sweep_isolates_failed_refresh_from_other_creators(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end contract: sweep_gmail_briefs iterates every
    Gmail-eligible creator, calls access_token_for_google per row,
    and moves on when that returns None. A None from creator A must
    not blank creator B."""
    creators = [
        {"user_id": "u-A", "scopes": []},
        {"user_id": "u-B", "scopes": []},
    ]

    def _fake_list_creators(pred, *, limit):
        return creators

    monkeypatch.setattr(
        bot_jobs.oauth_connections,
        "list_creators_with_google_scope",
        _fake_list_creators,
    )
    # sweep short-circuits when Google OAuth isn't configured in the
    # test env; force it on so we exercise the per-creator loop.
    monkeypatch.setattr(bot_jobs.google_calendar, "is_configured", lambda: True)

    # u-A: refresh permanently rejected → helper returns None → sweep skips.
    # u-B: valid token → sweep tries Gmail (we stub the Gmail call to
    #       return an empty list so we don't drift into filter code).
    token_calls: list[str] = []

    def _fake_access_token(user_id: str) -> str | None:
        token_calls.append(user_id)
        return None if user_id == "u-A" else "tok-B"

    monkeypatch.setattr(
        bot_jobs.oauth_connections,
        "access_token_for_google",
        _fake_access_token,
    )

    gmail_calls: list[str] = []

    def _fake_list_threads(token, *, limit):
        gmail_calls.append(token)
        return []

    monkeypatch.setattr(
        bot_jobs.google_gmail,
        "list_recent_threads",
        _fake_list_threads,
    )

    report = bot_jobs.sweep_gmail_briefs()

    assert token_calls == ["u-A", "u-B"], (
        "sweep must attempt every creator even after one returns None"
    )
    assert gmail_calls == ["tok-B"], (
        "creator A's dead credential must not be handed to Gmail; "
        "creator B's fresh credential must proceed"
    )
    assert report.job_name == "sweep_gmail_briefs"


# ---------------------------------------------------------------------------
# 7. Existing working connection behavior is unchanged (happy path).
# ---------------------------------------------------------------------------


def test_working_creator_behavior_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The 5th creator in the production incident — valid access_token,
    valid refresh_token, unexpired expiry — must behave EXACTLY as
    before the fix. This test guards that we didn't accidentally
    change the happy path while fixing the failure path."""
    _stub_connection(
        monkeypatch,
        {
            "access_token": "working-token",
            "refresh_token": "rt-good",
            "expires_at": _future_iso(3600),
        },
    )
    refresh_calls = _stub_refresh(monkeypatch, RuntimeError("must not refresh"))
    save_calls = _stub_save(monkeypatch, ok=True)

    token = oauth_connections.access_token_for_google("u-5")

    assert token == "working-token"
    assert refresh_calls == []
    assert save_calls == []


# ---------------------------------------------------------------------------
# 8. _post_token surfaces the permanent-reject typed error correctly.
#    This is the boundary between httpx and our helpers; getting it
#    right is what enables the caller to distinguish transient from
#    permanent.
# ---------------------------------------------------------------------------


class _FakeHTTPResponse:
    def __init__(self, *, status: int, body: dict | str) -> None:
        self.status_code = status
        self._body = body

    def json(self) -> Any:
        if isinstance(self._body, str):
            raise ValueError("not json")
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=httpx.Request("POST", "https://oauth2.googleapis.com/token"),
                response=httpx.Response(self.status_code),
            )


def test_post_token_raises_rejected_on_invalid_grant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        google_calendar.httpx,
        "post",
        lambda *a, **kw: _FakeHTTPResponse(status=400, body={"error": "invalid_grant"}),
    )
    with pytest.raises(google_calendar.GoogleTokenRefreshRejectedError):
        google_calendar._post_token({"grant_type": "refresh_token"})


def test_post_token_raises_generic_on_5xx(monkeypatch: pytest.MonkeyPatch) -> None:
    """A transient upstream 500 must NOT get classified as permanent —
    the caller might retry on the next tick and succeed."""
    monkeypatch.setattr(
        google_calendar.httpx,
        "post",
        lambda *a, **kw: _FakeHTTPResponse(status=500, body={"error": "internal"}),
    )
    with pytest.raises(google_calendar.GoogleCalendarError) as excinfo:
        google_calendar._post_token({"grant_type": "refresh_token"})
    assert not isinstance(excinfo.value, google_calendar.GoogleTokenRefreshRejectedError)


def test_post_token_raises_generic_on_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _blow(*a, **kw):
        raise httpx.ConnectError("dns fail")

    monkeypatch.setattr(google_calendar.httpx, "post", _blow)
    with pytest.raises(google_calendar.GoogleCalendarError) as excinfo:
        google_calendar._post_token({"grant_type": "refresh_token"})
    assert not isinstance(excinfo.value, google_calendar.GoogleTokenRefreshRejectedError)


def test_post_token_refresh_rejected_is_subclass_of_google_calendar_error() -> None:
    """Existing catch sites that do `except GoogleCalendarError` still
    catch the new subclass — a critical invariant for callers that
    only want 'refresh didn't work' semantics."""
    assert issubclass(
        google_calendar.GoogleTokenRefreshRejectedError,
        google_calendar.GoogleCalendarError,
    )
