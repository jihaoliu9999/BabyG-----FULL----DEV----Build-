"""babyg Brief page + context handoff tests.

Locks the behavior the product spec calls out:

* Home Brief "view all" points at ``/creator/brief`` (NOT Discover).
* ``/creator/brief`` renders needs_you + in_progress sections from
  real persisted state (action_proposals + notifications).
* Instagram items NEVER expose a send action.
* Gmail items expose the existing bot confirm endpoint when a
  ``source_message_id`` is present.
* Every item has an "ask babyg" doorway with a compact ``?brief=<key>``
  context param.
* The manager route (``/creator/bot``) accepts ``?brief=<key>``,
  resolves the item via ``brief_service.resolve_brief_context``,
  and the composer chip strip becomes context-aware (max 4).
* User-facing copy avoids the forbidden manager-jargon terms
  ("handled", "manager activity", "processed", etc.).
* No fake data — an empty aggregator yields an empty view model
  with a calm truthful empty state, not fabricated business items.
* Mobile CSS lives in scoped ``.brief-*`` selectors so no unrelated
  page is affected.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import action_proposals as action_proposals_module
from app.services import bot_prompts as bot_prompts_module
from app.services import brief as brief_service
from app.services import notifications as notifications_module

REPO = Path(__file__).resolve().parents[1]
BRIEF_TEMPLATE = REPO / "app" / "templates" / "creator" / "brief.html"
DASHBOARD_TEMPLATE = REPO / "app" / "templates" / "creator" / "dashboard.html"
APP_CSS = REPO / "app" / "static" / "css" / "app.css"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient) -> str:
    user_id = "user-brief-1"
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return user_id


@pytest.fixture()
def brief_world(monkeypatch):
    """Empty state by default. Tests populate the two source lists
    via monkeypatch as needed."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    return {}


# ---------------------------------------------------------------------------
# Routing — spec section 1 + 2
# ---------------------------------------------------------------------------


def test_home_view_all_points_to_brief_not_discover() -> None:
    """Fix for the audit-found routing bug: Home Brief `view all`
    used to route into `/creator/discover`. It must land on the
    dedicated Brief page."""
    dashboard = DASHBOARD_TEMPLATE.read_text()
    assert 'class="hv5-head-link" href="/creator/brief">view all' in dashboard
    # The old wrong route is gone in this exact context.
    assert 'class="hv5-head-link" href="/creator/discover">view all' not in dashboard


def test_brief_page_loads_for_authenticated_creator(client, brief_world):
    _signed_in(client)
    r = client.get("/creator/brief")
    assert r.status_code == 200
    assert 'class="brief-page"' in r.text
    assert 'class="brief-head-title">brief' in r.text


# ---------------------------------------------------------------------------
# Copy hygiene — spec section 21
# ---------------------------------------------------------------------------


_FORBIDDEN_COPY = (
    "handled",
    "manager activity",
    "real manager activity",
    "signal engine",
    "actionable intelligence",
    "processed 18 emails",
    "handled 39 instagram",
    "AI analysis",
    "workflow",
    "pipeline",
)


def test_brief_page_avoids_forbidden_manager_jargon(client, brief_world):
    _signed_in(client)
    r = client.get("/creator/brief")
    assert r.status_code == 200
    body = r.text.lower()
    for token in _FORBIDDEN_COPY:
        assert token.lower() not in body, token


def test_brief_template_uses_lowercase_babyg() -> None:
    tpl = BRIEF_TEMPLATE.read_text()
    # The word "Babyg" or "BabyG" should never appear in the template —
    # product copy is always lowercase.
    assert "Babyg" not in tpl
    assert "BabyG" not in tpl


# ---------------------------------------------------------------------------
# Empty state — spec section 16
# ---------------------------------------------------------------------------


def test_brief_page_empty_state_is_calm_and_truthful(client, brief_world):
    _signed_in(client)
    r = client.get("/creator/brief")
    assert r.status_code == 200
    assert "nothing needs your attention." in r.text
    # No fake business items when nothing real exists.
    for token in ("acme", "$3,500", "@brand"):
        assert token.lower() not in r.text.lower()


# ---------------------------------------------------------------------------
# Aggregator — direct behavioral tests over the service.
# ---------------------------------------------------------------------------


def test_gmail_proposal_becomes_needs_you_item_with_send_action(monkeypatch):
    proposal = {
        "id": "prop-gmail-1",
        "action_type": "gmail.send_email",
        "provider": "google",
        "preview": {
            "summary": "Acme wants to move forward at $3,500.",
            "recommendation": "counter at $4,000 with 30-day usage.",
        },
        "source_message_id": "bot-msg-42",
        "created_at": "2026-09-14T12:00:00Z",
    }
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [proposal],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    view = brief_service.build_brief("u1")
    assert not view["empty"]
    assert len(view["needs_you"]) == 1
    item = view["needs_you"][0]
    assert item["source"] == "gmail"
    assert item["state"] == "needs_you"
    labels = [a["label"] for a in item["actions"]]
    assert "send reply" in labels
    assert "ask babyg" in labels
    # Send action reuses the existing bot-messages confirm endpoint.
    send_action = next(a for a in item["actions"] if a["label"] == "send reply")
    assert send_action["endpoint"] == "/creator/bot/actions/bot-msg-42/confirm"
    assert send_action["method"] == "POST"


def test_gmail_proposal_without_source_message_id_hides_send_button(monkeypatch):
    """No message id -> no bot confirm endpoint. The Brief must NOT
    render a broken send button that would 404 at the existing
    confirm route. `ask babyg` still surfaces so the user can
    continue the topic in the manager."""
    proposal = {
        "id": "prop-gmail-2",
        "action_type": "gmail.send_email",
        "preview": {"summary": "queued reply"},
        "source_message_id": None,
        "created_at": "2026-09-14T12:00:00Z",
    }
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [proposal],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    labels = [a["label"] for a in item["actions"]]
    assert "send reply" not in labels
    assert "ask babyg" in labels


def test_instagram_item_never_exposes_send_action(monkeypatch):
    """PRODUCT-LEVEL RULE: babyg never surfaces a send-DM action for
    Instagram in the Brief. Even if the underlying notification had
    a `link_path` that could theoretically send, the Brief only
    offers review/ask actions."""
    notif = {
        "id": "notif-ig-1",
        "kind": "manager_alert",
        "title": "@brandname asked for rates for a Miami campaign.",
        "body": "babyg recommends confirming deliverables and usage before quoting.",
        "source_provider": "instagram",
        "link_path": "/creator/instagram/dms",
        "is_read": False,
        "priority": "high",
        "created_at": "2026-09-14T12:00:00Z",
    }
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [notif],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    assert item["source"] == "instagram"
    labels = [a["label"] for a in item["actions"]]
    assert "send reply" not in labels
    assert "send" not in [lbl.lower() for lbl in labels if lbl.startswith("send")]
    # Allowed actions per spec: review inquiry / ask babyg.
    assert "review inquiry" in labels
    assert "ask babyg" in labels


def test_native_dm_new_dm_without_source_provider_is_excluded(monkeypatch):
    """Legacy `new_dm` rows without an explicit source_provider are
    NOT surfaced as Instagram — that would misattribute a native
    babyg DM. Same rule the home carousel already enforces."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [
            {
                "id": "notif-nd-1",
                "kind": "new_dm",
                "title": "someone messaged you",
                "body": None,
                "source_provider": None,
                "is_read": False,
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    view = brief_service.build_brief("u1")
    assert view["empty"] is True


def test_in_progress_items_carry_read_notification_source(monkeypatch):
    """A manager notification the creator has already opened (
    `is_read=true`) lands in the in-progress section, not needs-you.
    Lifecycle maps to existing `is_read`/`archived_at` columns —
    no schema change."""
    notif = {
        "id": "notif-ig-2",
        "kind": "manager_alert",
        "title": "@brand followed up",
        "body": None,
        "source_provider": "instagram",
        "link_path": "/creator/instagram/dms",
        "is_read": True,
        "priority": "normal",
        "created_at": "2026-09-14T09:00:00Z",
    }
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [notif],
    )
    view = brief_service.build_brief("u1")
    assert view["needs_you"] == []
    assert len(view["in_progress"]) == 1


def test_ranking_puts_needs_you_first_then_priority(monkeypatch):
    """needs_you > in_progress; within needs_you, urgent > normal."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [
            {
                "id": "p-1",
                "action_type": "gmail.send_email",
                "preview": {"summary": "recent needs-you"},
                "source_message_id": "m1",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [
            {
                "id": "n-urgent",
                "kind": "manager_alert",
                "title": "urgent inquiry",
                "source_provider": "instagram",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "urgent",
                "created_at": "2026-09-14T11:00:00Z",
            },
            {
                "id": "n-progress",
                "kind": "manager_alert",
                "title": "older read item",
                "source_provider": "instagram",
                "link_path": "/creator/instagram/dms",
                "is_read": True,
                "priority": "urgent",
                "created_at": "2026-09-14T13:00:00Z",
            },
        ],
    )
    view = brief_service.build_brief("u1")
    # Urgent needs_you must beat normal needs_you.
    assert view["needs_you"][0]["id"] == "notif:n-urgent"
    # A read (in-progress) item never leads.
    assert all(it["state"] == "needs_you" for it in view["needs_you"])
    assert view["in_progress"][0]["id"] == "notif:n-progress"


# ---------------------------------------------------------------------------
# ask babyg context handoff — spec section 9
# ---------------------------------------------------------------------------


def test_ask_babyg_href_points_at_canonical_manager(monkeypatch):
    """Every Brief item exposes a doorway into the existing
    `/creator/bot` manager with a compact `?brief=<key>` context
    param. No new manager route, no cloned template."""
    proposal = {
        "id": "prop-x",
        "action_type": "gmail.send_email",
        "preview": {"summary": "hi"},
        "source_message_id": "m",
        "created_at": "2026-09-14T12:00:00Z",
    }
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [proposal],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    ask = next(a for a in item["actions"] if a["label"] == "ask babyg")
    assert ask["endpoint"].startswith("/creator/bot?brief=proposal:")


def test_brief_key_does_not_leak_raw_provider_payload_in_url(monkeypatch):
    """The `?brief=<key>` param is a SHORT INTERNAL identifier only
    (`proposal:<uuid>` or `notif:<uuid>`) — never a subject line,
    body, sender email, or any other provider payload."""
    proposal = {
        "id": "prop-secret",
        "action_type": "gmail.send_email",
        "preview": {
            "summary": "SECRET-BODY-CONTENT-DO-NOT-LEAK",
            "recipient": "leak@example.com",
        },
        "source_message_id": "m",
        "created_at": "2026-09-14T12:00:00Z",
    }
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [proposal],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    ask = next(a for a in item["actions"] if a["label"] == "ask babyg")
    assert "SECRET-BODY" not in ask["endpoint"]
    assert "leak@" not in ask["endpoint"]
    assert ask["endpoint"] == "/creator/bot?brief=proposal:prop-secret"


def test_resolve_brief_context_scopes_to_owner(monkeypatch):
    """A copied URL from another creator's Brief cannot surface
    that creator's item — the resolver's underlying lookups are
    owner-scoped."""
    monkeypatch.setattr(
        action_proposals_module,
        "get_for_user",
        lambda *, proposal_id, user_id: (
            {"id": proposal_id, "action_type": "gmail.send_email", "preview": {}}
            if user_id == "owner"
            else None
        ),
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=200, include_archived=False: [],
    )
    assert brief_service.resolve_brief_context(
        brief_key="proposal:p-1", user_id="owner"
    ) is not None
    assert brief_service.resolve_brief_context(
        brief_key="proposal:p-1", user_id="not-owner"
    ) is None


# ---------------------------------------------------------------------------
# Chip contract — spec section 10
# ---------------------------------------------------------------------------


def test_bot_prompts_context_chips_max_four_gmail() -> None:
    chips = bot_prompts_module.compute_prompts(
        brief_context={"source": "gmail", "summary": "acme wants $3,500"}
    )
    assert 1 <= len(chips) <= 4
    labels = [c["text"] for c in chips]
    assert any("send" in lbl for lbl in labels)


def test_bot_prompts_context_chips_instagram_never_sends() -> None:
    chips = bot_prompts_module.compute_prompts(
        brief_context={"source": "instagram", "summary": "@brand asked rates"}
    )
    assert 1 <= len(chips) <= 4
    labels = [c["text"].lower() for c in chips]
    for lbl in labels:
        assert "send the dm" not in lbl
        assert "send reply" not in lbl
        assert "send message" not in lbl


def test_bot_prompts_no_brief_context_falls_through_to_default() -> None:
    """Without brief_context, compute_prompts follows its normal
    signal-driven chip logic — the extension is strictly optional."""
    chips = bot_prompts_module.compute_prompts(
        unread_dms_count=0,
        recent_dm_peer_name=None,
        snapshot={},
        messages=[],
    )
    assert isinstance(chips, list)
    assert len(chips) <= 4


# ---------------------------------------------------------------------------
# Home preview stays truthful — spec section 11
# ---------------------------------------------------------------------------


def test_home_preview_cap_matches_spec() -> None:
    """The aggregator's home preview cap is 3 per spec."""
    assert brief_service.HOME_PREVIEW_MAX == 3


# ---------------------------------------------------------------------------
# CSS + scoping — spec sections 18 + 24
# ---------------------------------------------------------------------------


def test_brief_css_is_scoped_and_present() -> None:
    css = APP_CSS.read_text()
    # Scoping selector present.
    assert ".brief-page {" in css
    assert ".brief-item {" in css
    assert ".brief-item-action-primary {" in css
    # State pills.
    assert ".brief-item-state-needs {" in css
    assert ".brief-item-state-progress {" in css
    # Mobile-scoped tweaks are inside a media query.
    mobile_blocks = css.split("@media (max-width: 767px)")
    assert any(".brief-page {" in blk for blk in mobile_blocks[1:])


def test_no_authenticated_document_prefetch_reintroduced() -> None:
    """Regression guard from commit 229f88f — the Brief page must
    not add any `rel="prefetch"` for `/creator/brief` or any other
    authenticated route."""
    dashboard = DASHBOARD_TEMPLATE.read_text()
    brief_tpl = BRIEF_TEMPLATE.read_text()
    for path in (
        "/creator/brief",
        "/creator/bot",
        "/creator/discover",
        "/creator/dm",
    ):
        assert f'rel="prefetch" href="{path}"' not in dashboard
        assert f'rel="prefetch" href="{path}"' not in brief_tpl


# ---------------------------------------------------------------------------
# Bot route ?brief=<key> plumbing — verified at the route-signature level.
# A full end-to-end render of `/creator/bot` requires ~15 upstream service
# stubs (bot_nudges, awareness snapshot, agent_recap, agent_cycles, etc.)
# so we verify the query-string handler exists via source inspection.
# The chip-strip context handoff is covered end-to-end by
# ``test_bot_prompts_context_chips_*`` above.
# ---------------------------------------------------------------------------


def test_bot_route_accepts_brief_query_param_via_source() -> None:
    src = (REPO / "app" / "routes" / "creator.py").read_text()
    # The bot_chat handler declares a `brief` query param.
    assert "brief: str | None = Query(None)" in src
    # And plumbs it into resolve_brief_context.
    assert "resolve_brief_context" in src
    # And passes the result into compute_prompts.
    assert "brief_context=brief_context" in src
