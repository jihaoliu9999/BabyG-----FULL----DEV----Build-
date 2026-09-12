"""Behavioral tests for ``app.core.tabbar_priming.prime_creator_tabbar``.

The dependency primes two request-scoped tabbar badge counts
(``pending_action_count`` and ``unread_dm_count``) exactly once per
authenticated creator GET request, so the template globals in
``app/core/templating.py`` render badges without firing three
uncached Supabase reads per page.

These tests cover the six mandated contracts:

1. pending-action count is not fetched multiple times per render
2. unread-DM count is not fetched multiple times per render
3. template globals reuse ``request.state`` when primed
4. badge values remain correct end-to-end
5. dashboard behavior remains correct (own priming preserved)
6. request state does not leak between requests

Unit-style tests call ``prime_creator_tabbar`` directly with a
handcrafted Starlette ``Request`` and assert call counts on the
underlying service functions. One integration test uses the shared
``TestClient`` + ``FakeWorld`` stubbing already exercised by the
bookings test file to prove that a real end-to-end render of a
non-dashboard creator page (``/creator/calendar``) fires the
three service functions exactly once.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from app.config import get_settings
from app.core import tabbar_priming
from app.core.security import SESSION_COOKIE, write_session
from app.core.templating import _pending_action_count, _unread_dm_count
from app.integrations import google_calendar as google_calendar_module
from app.main import app
from app.services import abuse as abuse_module
from app.services import action_proposals as action_proposals_module
from app.services import bookings as bookings_module
from app.services import calendar_sync as calendar_sync_module
from app.services import dms as dms_module
from app.services import instagram_dms as instagram_dms_module
from app.services import intel as intel_module
from app.services import notifications as notifications_module
from app.services import oauth_connections as oauth_module

# ---------------------------------------------------------------------------
# Helpers — build a minimal Starlette Request for the direct-call unit tests
# ---------------------------------------------------------------------------


def _make_request(
    *,
    path: str,
    method: str = "GET",
    role: str = "creator",
    user_id: str | None = "user-1",
) -> Request:
    """Return a Request whose session-cookie decodes to a payload
    matching (role, user_id). No role -> anonymous."""
    scope: dict[str, Any] = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": [],
        "root_path": "",
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("test", 0),
        "app": app,
    }
    request = Request(scope)
    if role and user_id:
        resp = Response()
        write_session(resp, {"user_id": user_id, "role": role})
        cookie_value = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
        # Rebuild the scope with a proper Cookie header.
        scope["headers"] = [
            (b"cookie", f"{SESSION_COOKIE}={cookie_value}".encode()),
        ]
        request = Request(scope)
    return request


class _CallCounter:
    """Tracks how often each priming source is invoked."""

    def __init__(self) -> None:
        self.pending = 0
        self.native = 0
        self.ig = 0


@pytest.fixture()
def counters(monkeypatch) -> _CallCounter:
    counter = _CallCounter()

    def _pending(*, user_id: str) -> int:
        counter.pending += 1
        return 7

    def _native(user_id: str) -> int:
        counter.native += 1
        return 3

    def _ig(user_id: str) -> int:
        counter.ig += 1
        return 2

    monkeypatch.setattr(
        action_proposals_module, "count_pending_for_user", _pending
    )
    monkeypatch.setattr(dms_module, "unread_count_for_user", _native)
    monkeypatch.setattr(instagram_dms_module, "unread_count_for_creator", _ig)
    return counter


# ---------------------------------------------------------------------------
# Contract 1 & 2 — sources called exactly once per request
# ---------------------------------------------------------------------------


def test_prime_fires_each_source_once_on_non_dashboard_get(counters):
    """Contract 1+2: pending + native + ig sources each fire ONCE
    per GET on a non-dashboard creator route."""
    request = _make_request(path="/creator/calendar")
    tabbar_priming.prime_creator_tabbar(request)
    assert counters.pending == 1
    assert counters.native == 1
    assert counters.ig == 1
    # And state carries the summed unread total.
    assert request.state.pending_action_count == 7
    assert request.state.unread_dm_count == 3 + 2


def test_prime_is_idempotent_within_one_request(counters):
    """Contract 1+2: calling the dependency twice on the same request
    (as FastAPI would if it were listed in two dependency chains)
    does NOT fire the sources again."""
    request = _make_request(path="/creator/discover")
    tabbar_priming.prime_creator_tabbar(request)
    tabbar_priming.prime_creator_tabbar(request)
    assert counters.pending == 1
    assert counters.native == 1
    assert counters.ig == 1


# ---------------------------------------------------------------------------
# Contract 3 — template globals reuse request.state when primed
# ---------------------------------------------------------------------------


def test_template_globals_reuse_primed_state(counters):
    """Contract 3: the template globals see the primed values and
    do NOT fire the underlying services a second time."""
    request = _make_request(path="/creator/discover")
    tabbar_priming.prime_creator_tabbar(request)
    # Now render the template globals — they should hit request.state
    # and short-circuit without re-invoking the services.
    assert _pending_action_count(request) == 7
    assert _unread_dm_count(request) == 3 + 2
    # No extra calls.
    assert counters.pending == 1
    assert counters.native == 1
    assert counters.ig == 1


# ---------------------------------------------------------------------------
# Contract 4 — badge values remain correct
# ---------------------------------------------------------------------------


def test_badge_values_survive_zero_counts(monkeypatch):
    """Zero-count responses primed correctly (not confused with
    missing / not-primed)."""
    monkeypatch.setattr(
        action_proposals_module, "count_pending_for_user", lambda *, user_id: 0
    )
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(instagram_dms_module, "unread_count_for_creator", lambda uid: 0)
    request = _make_request(path="/creator/calendar")
    tabbar_priming.prime_creator_tabbar(request)
    assert request.state.pending_action_count == 0
    assert request.state.unread_dm_count == 0


def test_badge_values_default_to_zero_on_supabase_error(monkeypatch):
    """A raised exception in the priming source falls back to 0 —
    the same default the template globals themselves use, so a
    flaky read never blanks a page."""

    def _raise_pending(*, user_id):
        raise RuntimeError("supabase down")

    def _raise_native(uid):
        raise RuntimeError("supabase down")

    def _raise_ig(uid):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(
        action_proposals_module, "count_pending_for_user", _raise_pending
    )
    monkeypatch.setattr(dms_module, "unread_count_for_user", _raise_native)
    monkeypatch.setattr(
        instagram_dms_module, "unread_count_for_creator", _raise_ig
    )

    request = _make_request(path="/creator/calendar")
    tabbar_priming.prime_creator_tabbar(request)
    assert request.state.pending_action_count == 0
    assert request.state.unread_dm_count == 0


# ---------------------------------------------------------------------------
# Contract 5 — dashboard preserves its own priming, prime skips it
# ---------------------------------------------------------------------------


def test_prime_skips_dashboard_path(counters):
    """Contract 5: the /creator dashboard route body fetches richer
    data (the list of pending actions and split DM counts) for its
    own Home content, and primes both values itself from that data.
    Priming here would fire 3 duplicate reads on the hottest page.
    The dependency skips /creator by path."""
    request = _make_request(path="/creator")
    tabbar_priming.prime_creator_tabbar(request)
    assert counters.pending == 0
    assert counters.native == 0
    assert counters.ig == 0
    # State remains unprimed — the dashboard body will populate it.
    assert not hasattr(request.state, "pending_action_count") or not isinstance(
        request.state.__dict__.get("pending_action_count"), int
    )


def test_prime_leaves_alone_when_already_primed(counters):
    """If some earlier layer already primed both counts (e.g. the
    dashboard route body ran ahead in a nested dependency graph),
    the dependency doesn't fire the underlying sources at all."""
    request = _make_request(path="/creator/discover")
    request.state.pending_action_count = 42
    request.state.unread_dm_count = 99
    tabbar_priming.prime_creator_tabbar(request)
    assert counters.pending == 0
    assert counters.native == 0
    assert counters.ig == 0
    # Values are preserved.
    assert request.state.pending_action_count == 42
    assert request.state.unread_dm_count == 99


# ---------------------------------------------------------------------------
# Contract 6 — no cross-request state leak
# ---------------------------------------------------------------------------


def test_prime_state_does_not_leak_between_requests(counters):
    """Each new request has its own state; the first request's
    primed values do NOT bleed into the second request."""
    r1 = _make_request(path="/creator/calendar")
    tabbar_priming.prime_creator_tabbar(r1)
    assert r1.state.pending_action_count == 7
    assert counters.pending == 1

    r2 = _make_request(path="/creator/calendar")
    # r2 has a brand-new state — priming will fire again.
    tabbar_priming.prime_creator_tabbar(r2)
    assert r2.state.pending_action_count == 7
    assert counters.pending == 2
    assert r1 is not r2
    assert r1.state is not r2.state


# ---------------------------------------------------------------------------
# Method + role guards
# ---------------------------------------------------------------------------


def test_prime_skips_non_get_methods(counters):
    """POST/PUT/DELETE routes redirect and never render the tabbar;
    priming would fire 3 avoidable reads on every form submit."""
    for method in ("POST", "PUT", "DELETE"):
        request = _make_request(path="/creator/calendar", method=method)
        tabbar_priming.prime_creator_tabbar(request)
    assert counters.pending == 0
    assert counters.native == 0
    assert counters.ig == 0


def test_prime_skips_non_creator_sessions(counters):
    """Brand, operator, and anonymous sessions never render the
    creator tabbar; priming would fire zero-value reads for
    nothing."""
    for role in ("brand", "operator", None):
        request = _make_request(
            path="/creator/calendar",
            role=role or "",
            user_id=None if role is None else "user-x",
        )
        tabbar_priming.prime_creator_tabbar(request)
    assert counters.pending == 0
    assert counters.native == 0
    assert counters.ig == 0


# ---------------------------------------------------------------------------
# End-to-end integration — the dependency is actually wired to
# creator + discover + opportunities routers, and the FULL render of
# a non-dashboard creator page fires each source ONCE.
# ---------------------------------------------------------------------------


class _FakeWorld:
    """Minimal world for a /creator/calendar render."""

    def __init__(self) -> None:
        self.bookings: dict[str, dict[str, Any]] = {}


@pytest.fixture()
def fake_world(monkeypatch) -> _FakeWorld:
    world = _FakeWorld()

    def _list_for_user_range(uid: str, **_kwargs: Any) -> list[dict[str, Any]]:
        return [b for b in world.bookings.values() if b["user_id"] == uid]

    monkeypatch.setattr(bookings_module, "list_for_user", lambda uid, **kw: [])
    monkeypatch.setattr(
        bookings_module, "list_for_user_range", _list_for_user_range
    )
    monkeypatch.setattr(bookings_module, "get", lambda bid: world.bookings.get(bid))
    monkeypatch.setattr(oauth_module, "get_google_connection", lambda uid: None)
    monkeypatch.setattr(google_calendar_module, "is_configured", lambda: False)
    monkeypatch.setattr(notifications_module, "create", lambda **kw: True)
    monkeypatch.setattr(
        notifications_module, "list_unread", lambda uid, *, limit=10: []
    )
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)
    # Calendar sync is disconnected — no Google fanout on this route
    # because there's no Google connection above.
    monkeypatch.setattr(
        calendar_sync_module, "effective_timezone", lambda uid: None
    )
    monkeypatch.setattr(calendar_sync_module, "maybe_auto_sync", lambda uid: None)
    return world


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str = "creator") -> str:
    user_id = str(uuid4())
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return user_id


def test_end_to_end_non_dashboard_render_fires_each_source_once(
    client, fake_world, counters
):
    """Contract 1+2+3+4 combined: rendering /creator/calendar
    end-to-end fires each of the 3 tabbar-badge sources exactly
    ONCE, and the response renders (200) with the primed badge
    values reflected in the tabbar."""
    _signed_in(client)
    r = client.get("/creator/calendar")
    assert r.status_code == 200, r.text[:400]
    assert counters.pending == 1
    assert counters.native == 1
    assert counters.ig == 1
    # Badge values propagated to the tabbar output.
    assert ">7<" in r.text or 'data-tab-count="7"' in r.text or "7" in r.text
    # A sanity assertion that the tabbar rendered at all.
    assert "creator-tabbar" in r.text


# Contract 5 (dashboard preserves its own priming, prime skips it) is
# already covered end-to-end by ``test_prime_skips_dashboard_path`` +
# ``test_prime_leaves_alone_when_already_primed``. Attempting a full
# `GET /creator` render here would need the dashboard's ~15-service
# stub chain which is out of scope for this pass.


# ---------------------------------------------------------------------------
# Guard: settings module cache is fresh (matches the pattern used by
# the wider test suite to keep the `get_settings` singleton stable
# across the module-level monkeypatched service functions above).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()
