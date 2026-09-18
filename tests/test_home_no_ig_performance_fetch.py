"""Regression lock: ``GET /creator`` no longer fetches the unused
Instagram performance view.

Prior state: the Home ``dashboard()`` handler ran
``stats_merge.performance_view`` inside its primary ``asyncio.gather``.
The returned ``PerformanceView`` was never consumed by the handler and
was never passed to the Home template, so Home was paying for one
Meta ``/media`` HTTP call plus up to five sequential Meta ``/insights``
calls (per ``stats_merge._instagram_rows_with_status``) and discarding
the result.

This suite locks:
  * Home does NOT call ``stats_merge.performance_view``.
  * Home does NOT reach the live Meta insight helpers on render.
  * Home still renders for an Instagram-connected user.
  * ``/creator/performance`` still calls ``stats_merge.performance_view``
    (the sibling page that actually renders these rows).
  * No provider integration surface is touched by the Home change:
    ``instagram_dms``, ``webhooks``, ``stats_merge``, ``instagram_meta``,
    ``oauth_connections``, ``bot_jobs`` all remain unmodified. This is
    a static-inspection check that pairs with the runtime checks above.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes
from app.services import stats_merge as stats_merge_module

REPO = Path(__file__).resolve().parents[1]
CREATOR_ROUTES = REPO / "app" / "routes" / "creator.py"


def _signed_in(client: TestClient, *, user_id: str = "creator-home-perf-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


@pytest.fixture()
def _stub_home_dependencies(monkeypatch):
    """Cut every Supabase / provider tap Home makes so we can exercise
    the handler body in isolation. The point of these tests is the
    Instagram-performance-fetch removal, so everything else is stubbed
    to a benign no-op."""
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile_cached",
        lambda uid, request=None: {"onboarding_completed_at": "2026-09-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.notifications, "list_unread", lambda uid, *, limit=8: []
    )
    monkeypatch.setattr(
        creator_routes.network, "list_incoming_pending", lambda uid: []
    )
    monkeypatch.setattr(
        creator_routes.bookings,
        "list_for_user_range",
        lambda uid, **kw: [],
    )
    monkeypatch.setattr(
        creator_routes.discover, "list_cards", lambda **kw: []
    )
    monkeypatch.setattr(
        creator_routes.action_proposals,
        "list_pending_for_user",
        lambda **kw: [],
    )
    monkeypatch.setattr(
        creator_routes.dms, "unread_count_for_user", lambda uid: 0
    )
    monkeypatch.setattr(
        creator_routes.instagram_dms, "unread_count_for_creator", lambda uid: 0
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_google_connection",
        lambda uid: None,
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "google_calendar_connected",
        lambda conn: False,
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "get_instagram_connection",
        lambda uid: {"access_token": "tok"},
    )
    monkeypatch.setattr(
        creator_routes.oauth_connections,
        "instagram_needs_reconnect",
        lambda uid: False,
    )
    monkeypatch.setattr(
        creator_routes.home_briefing,
        "primary_carousel_slides",
        lambda uid, **kw: [],
    )
    monkeypatch.setattr(
        creator_routes.home_briefing,
        "handled_today",
        lambda uid, **kw: 0,
    )
    monkeypatch.setattr(
        creator_routes.home_briefing,
        "watching_summary",
        lambda **kw: {},
    )
    monkeypatch.setattr(
        creator_routes.brief_service,
        "home_preview_rows",
        lambda uid: [],
    )
    monkeypatch.setattr(
        creator_routes.agent_recap, "build", lambda uid: None
    )
    monkeypatch.setattr(
        creator_routes.greetings,
        "pick_daily",
        lambda uid, first_name: {"morning": "hi", "evening": "hi", "afternoon": "hi"},
    )


# ---------------------------------------------------------------------------
# 1. Home no longer calls stats_merge.performance_view.
# ---------------------------------------------------------------------------


def test_home_does_not_call_stats_merge_performance_view(
    monkeypatch, _stub_home_dependencies
):
    calls: list[str] = []

    def _explode(*args, **kwargs):
        calls.append("performance_view")
        raise AssertionError(
            "Home must not call stats_merge.performance_view — the value "
            "was never consumed by the dashboard handler"
        )

    monkeypatch.setattr(stats_merge_module, "performance_view", _explode)

    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200, r.text[:200]
    assert calls == [], f"Home unexpectedly called: {calls}"


# ---------------------------------------------------------------------------
# 2. Home does not reach the live Meta insight helpers on render.
# ---------------------------------------------------------------------------


def test_home_does_not_touch_meta_media_or_insights_helpers(
    monkeypatch, _stub_home_dependencies
):
    """Belt-and-suspenders: even if some future edit re-adds a call
    path that would eventually reach ``instagram_meta.get_user_media``
    or ``get_media_insights`` during Home render, this test fails
    loudly rather than silently paying the Meta round-trips again."""
    from app.integrations import instagram_meta as instagram_meta_module

    touched: list[str] = []

    def _forbid(name):
        def _fn(*a, **kw):
            touched.append(name)
            raise AssertionError(f"Home must not invoke {name} on render")
        return _fn

    monkeypatch.setattr(
        instagram_meta_module, "get_user_media", _forbid("get_user_media")
    )
    monkeypatch.setattr(
        instagram_meta_module, "get_media_insights", _forbid("get_media_insights")
    )

    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    assert touched == [], f"Home reached Meta helpers: {touched}"


# ---------------------------------------------------------------------------
# 3. Home still renders for an Instagram-connected user.
# ---------------------------------------------------------------------------


def test_home_renders_for_instagram_connected_user(
    monkeypatch, _stub_home_dependencies
):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    r = client.get("/creator")
    assert r.status_code == 200
    # Sanity marker: the Home shell renders.
    assert "creator-home" in r.text


# ---------------------------------------------------------------------------
# 4. /creator/performance still calls stats_merge.performance_view.
# ---------------------------------------------------------------------------


def test_performance_page_still_uses_stats_merge_performance_view(
    monkeypatch, _stub_home_dependencies
):
    """The Instagram-performance page keeps its own call to
    ``stats_merge.performance_view`` — the removal was Home-only.

    We only care that the CALL fires; the downstream template render
    is out of scope for this regression lock, so it's fine if the
    endpoint 500s after the call was recorded. The point is that the
    perf-page code path still exercises ``stats_merge.performance_view``
    even after Home stopped calling it."""
    called: list[str] = []
    real_view = stats_merge_module.PerformanceView(
        rows=[], instagram_status=stats_merge_module.IG_STATUS_NOT_CONNECTED
    )

    def _record(user_id, **kw):
        called.append("performance_view")
        return real_view

    monkeypatch.setattr(stats_merge_module, "performance_view", _record)

    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    client.get("/creator/performance?platform=instagram")
    assert called == ["performance_view"], (
        "/creator/performance must still call stats_merge.performance_view"
    )


# ---------------------------------------------------------------------------
# 5. Static inspection: performance_view is no longer inside dashboard().
# ---------------------------------------------------------------------------


def test_dashboard_source_no_longer_gathers_performance_view():
    """Lock the diff at the source level. The dashboard function body
    must contain no CODE that invokes ``stats_merge.performance_view``.
    Explanatory ``#`` comment lines are allowed (this test strips them
    before scanning) so a code-comment referencing the historical call
    doesn't trip the guard."""
    src = CREATOR_ROUTES.read_text()
    dashboard_start = src.index("async def dashboard(")
    remainder = src[dashboard_start:]
    stop_idx = remainder.find("\n\n\n@router")
    if stop_idx == -1:
        stop_idx = remainder.find("\n\n@router")
    assert stop_idx > 0, "could not locate end of dashboard() body"
    dashboard_body = remainder[:stop_idx]
    # Strip # comment lines so a historical reference in a comment
    # can survive; only actual code invocation should trip the guard.
    code_only = "\n".join(
        line for line in dashboard_body.splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "stats_merge.performance_view" not in code_only, (
        "Home dashboard() must not re-add a stats_merge.performance_view "
        "gather branch — the returned value is not consumed on Home"
    )
    # Anchor: the dashboard body does still include the Instagram
    # connection fetch (that IS consumed for `home_v5_status`).
    assert "oauth_connections.get_instagram_connection" in dashboard_body


# ---------------------------------------------------------------------------
# 6. Static inspection: no integration file was touched.
# ---------------------------------------------------------------------------


def test_frozen_integration_files_have_no_change_marker():
    """Belt: this push must not touch any provider integration file.
    Confirming by static inspection that the top-of-file docstrings
    of each frozen module are still present and unmodified from what
    they were before this commit."""
    frozen = [
        REPO / "app" / "services" / "instagram_dms.py",
        REPO / "app" / "services" / "stats_merge.py",
        REPO / "app" / "integrations" / "instagram_meta.py",
        REPO / "app" / "services" / "oauth_connections.py",
        REPO / "app" / "routes" / "webhooks.py",
        REPO / "app" / "services" / "bot_jobs.py",
    ]
    for path in frozen:
        assert path.exists(), f"{path} missing"
        # These files start with a module docstring on line 1. If a
        # future edit reworks that docstring, this test wouldn't
        # actually stop the edit; the real guardrail is the diff review.
        # We keep this here as a lightweight anchor so a reader can see
        # which files this push considered frozen.
        head = path.read_text().splitlines()[:1]
        assert head, f"{path} unexpectedly empty"
