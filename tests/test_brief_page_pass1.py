"""Brief page — visual pass 1 tests.

Scope: static UI only. Locks the small surface that Pass 1 owns:

* ``/creator/brief`` returns 200 for an authenticated onboarded
  creator.
* The rendered page contains the locked structural markers the approved
  Brief UI depends on: the vertical feed container, matter-type labels,
  `review` and `ask babyg` actions, and provider identity supplied by
  real service rows.
* The rendered page does NOT contain a page heading, section
  heads, tabs, or filter chips.
* Home Brief `view all` routes to ``/creator/brief``.
* No prototype data remains in the template.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.routes import creator as creator_routes
from app.services import brief as brief_service

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


def _card(platform: str, matter_type: str, *, urgent: bool = False) -> dict:
    return {
        "platform": platform,
        "platform_label": {
            "gmail": "Gmail",
            "instagram": "Instagram",
            "calendar": "Calendar",
            "babyg": "babyg",
        }[platform],
        "matter_type": matter_type,
        "urgent": urgent,
        "headline": f"real {platform} {matter_type}",
        "context": "persisted user-specific matter",
    }


def _stub_brief(monkeypatch, cards: list[dict] | None = None) -> None:
    monkeypatch.setattr(
        brief_service,
        "build_brief",
        lambda user_id: {
            "cards": list(cards or []),
            "empty": not bool(cards),
            "has_connected_provider": True,
        },
    )


def test_brief_route_resolves_for_onboarded_creator(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    _stub_brief(monkeypatch)
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
    _stub_brief(monkeypatch)
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
    _stub_brief(
        monkeypatch,
        [_card("gmail", matter_type) for matter_type in (
            "deal",
            "response",
            "decision",
            "follow-up",
            "booking",
            "update",
        )],
    )
    body = client.get("/creator/brief").text
    for matter_type in ("deal", "response", "decision", "follow-up", "booking", "update"):
        assert (
            f'data-brief-type="{matter_type}"' in body
        ), f"missing matter type card: {matter_type}"


def test_brief_cards_use_gmail_and_instagram(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    _stub_profile(monkeypatch)
    _stub_brief(
        monkeypatch,
        [
            _card("gmail", "decision"),
            _card("instagram", "deal"),
        ],
    )
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
    _stub_brief(
        monkeypatch,
        [_card("gmail", "decision"), _card("instagram", "deal")],
    )
    body = client.get("/creator/brief").text
    # Both actions render on every card. Not fewer, not more.
    review_count = body.count(">review<")
    ask_count = body.count(">ask babyg<")
    assert review_count == 2
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
    _stub_brief(
        monkeypatch,
        [
            _card("gmail", "decision", urgent=True),
            _card("instagram", "deal"),
            _card("gmail", "response"),
        ],
    )
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


def test_brief_route_uses_authenticated_user_for_service(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client, user_id="creator-brief-owner")
    _stub_profile(monkeypatch)

    calls: list[str] = []
    monkeypatch.setattr(
        brief_service,
        "build_brief",
        lambda user_id: calls.append(user_id)
        or {"cards": [], "empty": True, "has_connected_provider": True},
    )

    r = client.get("/creator/brief")
    assert r.status_code == 200
    assert calls == ["creator-brief-owner"]


def test_brief_template_contains_no_static_prototype_cards() -> None:
    tpl = BRIEF_TEMPLATE.read_text()
    for token in ("nike", "acme", "vault coffee", "studioverde", "rivetco"):
        assert token not in tpl.lower()
    assert "data-brief-prototype" not in tpl


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
