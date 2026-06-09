"""Instagram OAuth-connection storage helpers.

Mocks the Supabase service client at the boundary and asserts the
storage payloads + the state-signing contract. Step 2 scope:
backend helpers only, no UI, no bot tool.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.config import get_settings
from app.integrations import instagram_meta
from app.services import oauth_connections


@pytest.fixture(autouse=True)
def _reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# state round-trip (signed cookie protecting against CSRF on the callback)
# ---------------------------------------------------------------------------


def test_instagram_state_round_trip():
    state = oauth_connections.create_instagram_state(
        "creator-1", next_path="/creator/profile/settings"
    )
    out = oauth_connections.verify_instagram_state(state)
    assert out == {
        "user_id": "creator-1",
        "next": "/creator/profile/settings",
    }


def test_instagram_state_rejects_google_states():
    """A Google-signed state must NOT verify as an Instagram state.
    Different salts keep the providers from sharing tokens."""
    google_state = oauth_connections.create_google_state("creator-1")
    assert oauth_connections.verify_instagram_state(google_state) is None


def test_instagram_state_rejects_tampered_state():
    state = oauth_connections.create_instagram_state("creator-1")
    assert oauth_connections.verify_instagram_state(state + "x") is None
    assert oauth_connections.verify_instagram_state("") is None
    assert oauth_connections.verify_instagram_state("garbage") is None


# ---------------------------------------------------------------------------
# get_instagram_connection
# ---------------------------------------------------------------------------


class _FakeBuilder:
    """Thin chainable stand-in for the supabase-py builder."""

    def __init__(self, store: dict[str, Any]):
        self._store = store
        self._filters: list[tuple[str, Any]] = []
        self._table = ""

    def table(self, name: str):
        self._table = name
        return self

    def select(self, *_a, **_kw):
        self._store.setdefault("select", []).append(self._table)
        return self

    def insert(self, payload: Any):
        self._store.setdefault("inserts", []).append((self._table, payload))
        return self

    def upsert(self, payload: Any, **kwargs):
        self._store.setdefault("upserts", []).append(
            {"table": self._table, "payload": payload, "kwargs": kwargs}
        )
        return self

    def update(self, payload: Any):
        self._store.setdefault("updates", []).append(
            {"table": self._table, "payload": payload, "filters": list(self._filters)}
        )
        return self

    def delete(self):
        self._store.setdefault("deletes", []).append({"table": self._table})
        return self

    def eq(self, col: str, value: Any):
        self._filters.append((col, value))
        return self

    def limit(self, *_a, **_kw):
        return self

    def execute(self):
        data = self._store.get("execute_data", [])

        class _Result:
            def __init__(self, rows):
                self.data = rows

        return _Result(data)


def _patch_supabase(monkeypatch, store):
    builder = _FakeBuilder(store)
    monkeypatch.setattr(
        oauth_connections.supabase_client,
        "get_service_client",
        lambda: type("FakeClient", (), {"table": builder.table})(),
    )
    return builder


def test_get_instagram_connection_returns_row(monkeypatch):
    store = {"execute_data": [{"provider": "instagram", "access_token": "tok"}]}
    _patch_supabase(monkeypatch, store)
    row = oauth_connections.get_instagram_connection("creator-1")
    assert row == {"provider": "instagram", "access_token": "tok"}


def test_get_instagram_connection_returns_none_when_missing(monkeypatch):
    _patch_supabase(monkeypatch, {"execute_data": []})
    assert oauth_connections.get_instagram_connection("creator-1") is None


# ---------------------------------------------------------------------------
# save_instagram_connection
# ---------------------------------------------------------------------------


def test_save_instagram_connection_writes_expected_payload(monkeypatch):
    store: dict[str, Any] = {"execute_data": [{"x": 1}]}
    _patch_supabase(monkeypatch, store)
    account = instagram_meta.InstagramAccount(
        ig_user_id="ig-1784", username="miacreates", name="Mia"
    )
    ok = oauth_connections.save_instagram_connection(
        "creator-1",
        {
            "access_token": "long-token",
            "expires_in": 5184000,
        },
        ig_account=account,
    )
    assert ok is True
    assert len(store["upserts"]) == 1
    upsert = store["upserts"][0]
    assert upsert["table"] == "oauth_connections"
    payload = upsert["payload"]
    assert payload["user_id"] == "creator-1"
    assert payload["provider"] == "instagram"
    assert payload["access_token"] == "long-token"
    assert payload["scopes"] == list(instagram_meta.SCOPES)
    assert payload["provider_account_id"] == "ig-1784"
    assert payload["refresh_token"] is None
    assert payload["expires_at"]  # ISO timestamp
    assert upsert["kwargs"]["on_conflict"] == "user_id,provider"


def test_save_instagram_connection_refuses_when_token_missing(monkeypatch):
    store: dict[str, Any] = {}
    _patch_supabase(monkeypatch, store)
    account = instagram_meta.InstagramAccount(
        ig_user_id="ig-1", username=None, name=None
    )
    ok = oauth_connections.save_instagram_connection(
        "creator-1", {"access_token": ""}, ig_account=account
    )
    assert ok is False
    assert "upserts" not in store


# ---------------------------------------------------------------------------
# disconnect_instagram
# ---------------------------------------------------------------------------


def test_disconnect_instagram_deletes_only_instagram_row(monkeypatch):
    store: dict[str, Any] = {"execute_data": []}
    _patch_supabase(monkeypatch, store)
    assert oauth_connections.disconnect_instagram("creator-1") is True
    # The delete builder was used.
    assert store.get("deletes")


# ---------------------------------------------------------------------------
# access_token_for_instagram
# ---------------------------------------------------------------------------


def test_access_token_returns_none_when_no_connection(monkeypatch):
    _patch_supabase(monkeypatch, {"execute_data": []})
    assert oauth_connections.access_token_for_instagram("creator-1") is None


def test_access_token_returns_token_when_not_expired(monkeypatch):
    far_future = "2099-01-01T00:00:00+00:00"
    _patch_supabase(
        monkeypatch,
        {
            "execute_data": [
                {
                    "provider": "instagram",
                    "access_token": "valid-token",
                    "expires_at": far_future,
                }
            ]
        },
    )
    # Refresh should NOT be called.
    monkeypatch.setattr(
        instagram_meta,
        "refresh_long_lived_token",
        lambda _t: pytest.fail("refresh must not run for a fresh token"),
    )
    assert oauth_connections.access_token_for_instagram("creator-1") == "valid-token"


def test_access_token_refreshes_when_expired(monkeypatch):
    past = "2000-01-01T00:00:00+00:00"
    _patch_supabase(
        monkeypatch,
        {
            "execute_data": [
                {
                    "provider": "instagram",
                    "access_token": "stale-token",
                    "expires_at": past,
                }
            ]
        },
    )

    monkeypatch.setattr(
        instagram_meta,
        "refresh_long_lived_token",
        lambda _t: {"access_token": "fresh-token", "expires_in": 5184000},
    )
    assert oauth_connections.access_token_for_instagram("creator-1") == "fresh-token"


def test_access_token_falls_back_to_stale_on_refresh_failure(monkeypatch):
    past = "2000-01-01T00:00:00+00:00"
    _patch_supabase(
        monkeypatch,
        {
            "execute_data": [
                {
                    "provider": "instagram",
                    "access_token": "stale-token",
                    "expires_at": past,
                }
            ]
        },
    )

    def _boom(_t):
        raise instagram_meta.InstagramError("refresh failed")

    monkeypatch.setattr(instagram_meta, "refresh_long_lived_token", _boom)
    # Stale token still returned — the next Graph call will fail and
    # the bot tool surfaces `available: false`. Better than a hard
    # null which would hide a recoverable hiccup.
    assert oauth_connections.access_token_for_instagram("creator-1") == "stale-token"
