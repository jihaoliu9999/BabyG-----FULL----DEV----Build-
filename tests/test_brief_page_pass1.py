"""Brief page — visual pass 1 tests.

Scope: static UI only. Locks the small surface that Pass 1 owns:

* ``/creator/brief`` returns 200 for an authenticated onboarded
  creator.
* The rendered page contains the locked structural markers a
  design-only pass depends on: the vertical feed container,
  matter-type labels, `review` and `ask babyg` actions, both
  Gmail and Instagram examples.
* The rendered page does NOT contain a page heading, section
  heads, tabs, or filter chips.
* Home Brief `view all` routes to ``/creator/brief``.
* No production data is touched: the route never asks any
  service for card data.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes

REPO = Path(__file__).resolve().parents[1]
DASHBOARD_TEMPLATE = REPO / "app" / "templates" / "creator" / "dashboard.html"
BRIEF_TEMPLATE = REPO / "app" / "templates" / "creator" / "brief.html"
APP_CSS = REPO / "app" / "static" / "css" / "app.css"


def _signed_in(client: TestClient, *, user_id: str = "creator-brief-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def _stub_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile_cached",
        lambda uid, request: {"onboarding_completed_at": "2026-09-01T00:00:00Z"},
    )


def test_brief_route_resolves_for_onboarded_creator(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    r = client.get("/creator/brief")
    assert r.status_code == 200, r.text[:300]
    assert 'class="brief-feed"' in r.text
    assert 'data-page="brief-v1"' in r.text


def test_brief_route_redirects_when_not_onboarded(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile_cached",
        lambda uid, request: {"onboarding_completed_at": None},
    )
    r = client.get("/creator/brief")
    assert r.status_code == 302
    assert r.headers["location"] == "/onboarding/creator"


def test_brief_page_has_no_heading_no_sections_no_tabs_no_filters(monkeypatch):
    """Locked structural rule: the Brief page is a single feed. No
    hero, no section heads, no tab bar, no filter chips."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    body = client.get("/creator/brief").text
    tpl = BRIEF_TEMPLATE.read_text()
    # Neither the rendered response nor the template introduce a
    # page heading or forbidden section labels.
    for banned_phrase in (
        ">Brief<",
        ">Your Brief<",
        ">Daily Brief<",
        ">needs you<",
        ">caught up<",
        ">earlier today<",
    ):
        assert banned_phrase not in body, f"forbidden phrase in body: {banned_phrase!r}"
    # No <h1> or feed-level <h2>/<h3> inside the brief-feed container.
    feed_html = body.split('class="brief-feed"', 1)[1].split("</main>", 1)[0]
    assert "<h1" not in feed_html
    assert "<h2" not in feed_html
    assert "<h3" not in feed_html
    # No tab bar, no filter chips.
    assert 'role="tablist"' not in feed_html
    assert 'class="chip' not in feed_html
    assert 'data-filter' not in feed_html
    # Sanity: template file itself contains no page heading tokens.
    assert "block title" in tpl  # extends base.html correctly
    assert "brief-page-heading" not in tpl


def test_brief_cards_expose_locked_matter_types(monkeypatch):
    """Every one of the six locked matter types renders at least
    once so the visual reviewer can compare them side by side."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    body = client.get("/creator/brief").text
    for matter_type in ("deal", "response", "decision", "follow-up", "booking", "update"):
        assert (
            f'data-brief-type="{matter_type}"' in body
        ), f"missing matter type card: {matter_type}"


def test_brief_cards_use_gmail_and_instagram(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    body = client.get("/creator/brief").text
    assert 'data-brief-platform="gmail"' in body
    assert 'data-brief-platform="instagram"' in body
    # Platform labels rendered in the exact locked casing.
    assert ">Gmail<" in body
    assert ">Instagram<" in body


def test_brief_cards_render_review_and_ask_babyg_actions(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    body = client.get("/creator/brief").text
    # Both actions render on every card. Not fewer, not more.
    review_count = body.count(">review<")
    ask_count = body.count(">ask babyg<")
    assert review_count >= 6  # one per locked matter type at minimum
    assert review_count == ask_count
    # Actions are inert prototype anchors — no fake success wiring.
    for anchor_snippet in (
        'class="brief-card-action brief-card-action-primary" href="#" aria-disabled="true"',
        'class="brief-card-action brief-card-action-ghost" href="#" aria-disabled="true"',
    ):
        assert anchor_snippet in body


def test_brief_urgent_is_selective_not_generic(monkeypatch):
    """`urgent` marker must appear on a small number of cards, not
    every card. The design intent is that most cards are important
    business info, only a few are time-sensitive."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    body = client.get("/creator/brief").text
    urgent_count = body.count(">urgent<")
    card_count = body.count('class="brief-card"')
    assert urgent_count >= 1
    assert urgent_count < card_count
    assert urgent_count <= card_count // 2


def test_home_brief_view_all_routes_to_brief_page():
    """Home Brief `view all` link points at `/creator/brief`
    (not `/creator/discover`, the pre-Pass-1 destination)."""
    dashboard = DASHBOARD_TEMPLATE.read_text()
    assert 'class="hv5-head-link" href="/creator/brief">view all' in dashboard
    assert 'class="hv5-head-link" href="/creator/discover">view all' not in dashboard


def test_brief_route_never_calls_provider_services(monkeypatch):
    """Sentinel guard: the Brief route body must not invoke Gmail,
    Instagram, notifications, DMs, action_proposals, brief, or any
    other production service. Any such call in a static prototype
    render is out-of-scope for Pass 1."""
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)

    called: list[str] = []

    def _explode(name: str):
        def _fn(*a, **kw):
            called.append(name)
            raise AssertionError(f"Pass 1 must not call {name}")
        return _fn

    # Monkeypatch the Brief-card-data services a real Brief
    # implementation would touch. Any accidental use during this
    # visual pass trips the AssertionError and fails loudly.
    # Deliberately NOT included: the tabbar priming dependency
    # (`action_proposals.count_pending_for_user`, `dms.unread_count_
    # for_user`, `instagram_dms.unread_count_for_creator`). Those
    # are the shared creator shell's badge reads, not Brief card
    # data, and are pre-existing behavior on every creator page.
    for module_name, attr in (
        ("gmail", "list_recent_manager_threads"),
        ("gmail", "list_threads"),
        ("gmail", "sweep_gmail_briefs"),
        ("instagram_dms", "list_recent_manager_threads"),
        ("instagram_dms", "list_threads"),
        ("notifications", "list_for_user"),
        ("notifications", "list_unread"),
    ):
        try:
            module = __import__(f"app.services.{module_name}", fromlist=[attr])
        except (ImportError, AttributeError):
            continue
        if hasattr(module, attr):
            monkeypatch.setattr(module, attr, _explode(f"{module_name}.{attr}"))

    r = client.get("/creator/brief")
    assert r.status_code == 200
    assert called == [], f"pass 1 accidentally called: {called}"


def test_brief_css_is_scoped_and_present():
    """The visual pass ships its own scoped selectors. If the CSS
    is missing, the cards render but every card is unstyled — the
    review is meaningless. Lock the class names Pass 1 introduced."""
    css = APP_CSS.read_text()
    for cls in (
        ".brief-feed",
        ".brief-card",
        ".brief-card-top",
        ".brief-card-icon",
        ".brief-card-platform",
        ".brief-card-type",
        ".brief-card-urgent",
        ".brief-card-headline",
        ".brief-card-context",
        ".brief-card-actions",
        ".brief-card-action",
    ):
        assert cls + " {" in css or cls + "," in css, (
            f"scoped selector missing: {cls}"
        )
