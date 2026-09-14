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
    assert 'class="brief-head-title">brief' not in r.text
    assert 'class="brief-section-title"' not in r.text


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


def test_brief_template_uses_babyg_mark_not_generic_clock() -> None:
    tpl = BRIEF_TEMPLATE.read_text()
    assert '<img src="/static/assets/logo-bg.png" alt="" />' in tpl
    assert "M12 8v4l3 3" not in tpl


def test_brief_template_has_no_dead_href_patterns() -> None:
    tpl = BRIEF_TEMPLATE.read_text()
    assert 'href="#"' not in tpl
    assert "javascript:void" not in tpl
    assert "safe_url" in tpl


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


def test_brief_page_keeps_per_card_state_without_top_section_label(
    client, monkeypatch
):
    _signed_in(client)
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
                "id": "notif-ig-state",
                "kind": "manager_alert",
                "title": "@nike asked for campaign rates",
                "body": "Confirm deliverables and usage before quoting.",
                "source_provider": "instagram",
                "source_event_id": "instagram:message:m-1",
                "source_thread_id": "ig-thread-1",
                "underlying_type": "instagram_dm_message",
                "underlying_id": "msg-1",
                "link_path": "/creator/instagram/dms?thread=ig-thread-1",
                "is_read": False,
                "priority": "high",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    r = client.get("/creator/brief")
    assert r.status_code == 200
    assert 'class="brief-section-title"' not in r.text
    assert "brief-section-needs-label" not in r.text
    assert "brief-head-title" not in r.text
    assert "needs you" in r.text
    assert "@nike asked for campaign rates" in r.text


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


def test_instagram_new_dm_with_persisted_manager_context_is_brief_eligible(
    monkeypatch,
):
    """Meaningful Instagram DM notifications from the persisted
    manager path must appear in both the full Brief and Home preview."""
    notif = {
        "id": "notif-ig-business",
        "kind": "new_dm",
        "title": "@nike asked for campaign rates",
        "body": "Confirm deliverables and usage before quoting.",
        "source_provider": "instagram",
        "source_event_id": "instagram:message:m-business",
        "source_thread_id": "ig-thread-business",
        "underlying_type": "instagram_dm_message",
        "underlying_id": "ig-message-row",
        "link_path": "/creator/instagram/dms?thread=ig-thread-business",
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
    # Pass 3 §Platform casing (LOCKED): Instagram Title-cased.
    assert item["source_label"] == "Instagram"
    assert item["what_happened"] == "@nike asked for campaign rates"
    assert brief_service.home_preview_rows("u1")[0]["slot"] == "instagram"


def test_raw_instagram_unread_count_is_excluded(monkeypatch):
    """A raw unread-count row is not a business-evaluated Brief matter."""
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
                "id": "notif-ig-raw",
                "kind": "manager_alert",
                "title": "caught 2 new instagram dms",
                "body": None,
                "source_provider": "instagram",
                "metadata": {"summary_type": "raw_unread_count"},
                "is_read": False,
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    assert brief_service.build_brief("u1")["empty"] is True


def test_source_resolution_prefers_persisted_provider_metadata(monkeypatch):
    proposal = {
        "id": "prop-provider-meta",
        "action_type": "unknown.action",
        "provider": "babyg",
        "preview": {
            "source_provider": "instagram",
            "summary": "@agency asked about usage rights",
        },
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
    assert item["source"] == "instagram"
    # Pass 3 §Platform casing (LOCKED).
    assert item["source_label"] == "Instagram"


@pytest.mark.parametrize(
    ("source_provider", "kind", "underlying_type", "expected_source", "expected_label"),
    [
        ("instagram", "manager_alert", "instagram_dm_message", "instagram", "Instagram"),
        ("gmail", "manager_alert", "gmail_thread", "gmail", "Gmail"),
        (None, "booking_reminder", "booking", "calendar", "Calendar"),
        (None, "connection_request", "network_connection", "babyg", "babyg"),
    ],
)
def test_notification_platform_labels_from_persisted_source(
    monkeypatch,
    source_provider,
    kind,
    underlying_type,
    expected_source,
    expected_label,
):
    notif = {
        "id": f"notif-{expected_source}",
        "kind": kind,
        "title": f"{expected_label} persisted item",
        "body": None,
        "source_provider": source_provider,
        "underlying_type": underlying_type,
        "underlying_id": f"{expected_source}-1",
        "link_path": (
            "/creator/instagram/dms"
            if expected_source == "instagram"
            else "/creator/connections"
        ),
        "is_read": False,
        "priority": "normal",
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
    assert item["source"] == expected_source
    assert item["source_label"] == expected_label
    if expected_source != "babyg":
        assert item["source_label"] != "babyg"


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


def test_connection_request_heading_uses_identity_when_persisted(monkeypatch):
    notif = {
        "id": "notif-conn-jordan",
        "kind": "connection_request",
        "title": "Someone wants to connect.",
        "body": None,
        "source_provider": None,
        "metadata": {"requester_name": "Jordan"},
        "link_path": "/creator/connections",
        "is_read": False,
        "priority": "normal",
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
    assert item["source"] == "babyg"
    assert item["source_label"] == "babyg"
    assert item["what_happened"] == "New connection request from Jordan"


def test_connection_request_without_identity_uses_safe_specific_fallback(
    monkeypatch,
):
    notif = {
        "id": "notif-conn-generic",
        "kind": "connection_request",
        "title": "Someone wants to connect.",
        "body": None,
        "source_provider": None,
        "metadata": {},
        "link_path": "/creator/connections",
        "is_read": False,
        "priority": "normal",
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
    assert item["what_happened"] == "New connection request"
    assert "Someone wants to connect." not in item["what_happened"]


def test_unknown_notification_source_is_excluded_not_mapped_to_babyg(
    monkeypatch,
):
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
                "id": "notif-unknown",
                "kind": "manager_alert",
                "title": "Business activity",
                "body": None,
                "source_provider": None,
                "metadata": {},
                "link_path": "/creator/notifications",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    view = brief_service.build_brief("u1")
    assert view["empty"] is True


def test_unknown_action_proposal_source_is_excluded_not_mapped_to_babyg(
    monkeypatch,
):
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [
            {
                "id": "prop-unknown",
                "action_type": "unknown.action",
                "provider": "",
                "preview": {"summary": "unknown proposal"},
                "source_message_id": None,
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    view = brief_service.build_brief("u1")
    assert view["empty"] is True


def test_review_action_requires_valid_internal_destination(monkeypatch):
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
                "id": "notif-dead-review",
                "kind": "connection_request",
                "title": "New connection request",
                "body": None,
                "link_path": "#",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    assert "review" not in [a["label"] for a in item["actions"]]
    assert all(a["endpoint"] != "#" for a in item["actions"])
    assert all(not a["endpoint"].startswith("javascript:") for a in item["actions"])
    assert [a["label"] for a in item["actions"]] == ["ask babyg"]


def test_calendar_notification_uses_calendar_label_and_view_event_action(
    monkeypatch,
):
    notif = {
        "id": "notif-cal",
        "kind": "booking_reminder",
        "title": "Campaign call tomorrow at 2:00 PM",
        "body": None,
        "source_provider": None,
        "metadata": {"event_title": "Campaign call tomorrow at 2:00 PM"},
        "link_path": "/creator/calendar?date=2026-09-15",
        "is_read": False,
        "priority": "normal",
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
    assert item["source"] == "calendar"
    # Pass 3 §Platform casing (LOCKED).
    assert item["source_label"] == "Calendar"
    assert item["what_happened"] == "Campaign call tomorrow at 2:00 PM"
    assert any(a["label"] == "view event" for a in item["actions"])


def test_read_notification_stays_needs_you_but_marks_seen(monkeypatch):
    """Pass 2 §2 lifecycle correction: opening a notification does
    not mean the underlying business action has begun. A read
    notification stays ``needs_you`` and only flips its attention
    flag to ``seen=true``. Only real business-action state (
    ``action_proposals.status`` ∈ {confirmed, executing}) can move
    a Brief item to ``in_progress``."""
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
    assert view["in_progress"] == []
    assert len(view["needs_you"]) == 1
    item = view["needs_you"][0]
    assert item["state"] == "needs_you"
    assert item["seen"] is True


def test_ranking_puts_needs_you_first_then_priority(monkeypatch):
    """needs_you > in_progress; within needs_you, urgent > normal.
    Both notification rows now stay needs_you (seen/unseen carried
    separately); only the urgent one leads."""
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
                "id": "n-normal",
                "kind": "manager_alert",
                "title": "older normal item",
                "source_provider": "instagram",
                "link_path": "/creator/instagram/dms",
                "is_read": True,
                "priority": "normal",
                "created_at": "2026-09-14T13:00:00Z",
            },
        ],
    )
    view = brief_service.build_brief("u1")
    # Urgent needs_you must beat normal needs_you.
    assert view["needs_you"][0]["id"] == "notif:n-urgent"
    # Both notification rows stay needs_you (read != in_progress).
    assert all(it["state"] == "needs_you" for it in view["needs_you"])
    # No in_progress because no proposal is confirmed/executing.
    assert view["in_progress"] == []


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


def test_resolve_brief_context_uses_notification_source_and_heading(monkeypatch):
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=200, include_archived=False: [
            {
                "id": "notif-cal-context",
                "kind": "booking_reminder",
                "title": "Campaign call tomorrow at 2:00 PM",
                "body": None,
                "metadata": {"event_title": "Campaign call tomorrow at 2:00 PM"},
            }
        ],
    )
    ctx = brief_service.resolve_brief_context(
        brief_key="notif:notif-cal-context",
        user_id="owner",
    )
    assert ctx is not None
    assert ctx["source"] == "calendar"
    # Pass 3 §Platform casing (LOCKED).
    assert ctx["source_label"] == "Calendar"
    assert ctx["summary"] == "Campaign call tomorrow at 2:00 PM"


def test_resolve_brief_context_invalid_or_foreign_id_leaks_nothing(monkeypatch):
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=200, include_archived=False: [],
    )
    assert (
        brief_service.resolve_brief_context(
            brief_key="notif:foreign-secret", user_id="owner"
        )
        is None
    )
    assert brief_service.resolve_brief_context(brief_key="notif:", user_id="owner") is None


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
    assert ".brief-head-title" not in css
    assert ".brief-section-title" not in css
    source_label_block = css.split(".brief-item-source-label {", 1)[1].split("}", 1)[0]
    assert "text-transform" not in source_label_block
    assert "padding: max(4px, env(safe-area-inset-top, 0px))" in css
    # Mobile-scoped tweaks are inside a media query.
    mobile_blocks = css.split("@media (max-width: 767px)")
    assert any(".brief-page {" in blk for blk in mobile_blocks[1:])


def test_brief_layout_has_no_reserved_title_or_section_header_blocks() -> None:
    tpl = BRIEF_TEMPLATE.read_text()
    css = APP_CSS.read_text()
    assert "brief-head" not in tpl
    assert "brief-section-title" not in tpl
    assert "brief-section-needs-label" not in tpl
    assert ".brief-head" not in css


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


# ===========================================================================
# Pass 2 — Home unification, lifecycle correctness, matter grouping, and the
# manager context strip.
# ===========================================================================


# ---------------------------------------------------------------------------
# Home unification (Pass 2 §1)
# ---------------------------------------------------------------------------


def test_dashboard_route_consumes_brief_service_not_home_briefing_brief_rows() -> None:
    """Home Brief carousel must be sourced from the same aggregation
    as /creator/brief. The old `home_briefing.brief_rows()` path is
    no longer used to build the Home carousel — this locks the call
    site."""
    src = (REPO / "app" / "routes" / "creator.py").read_text()
    assert "brief_service.home_preview_rows" in src
    # And the old brief_rows(...) call is gone from the dashboard body.
    dashboard_body = src.split("async def dashboard", 1)[1].split(
        "\nasync def ", 1
    )[0]
    assert "home_briefing.brief_rows(" not in dashboard_body


def test_home_preview_rows_max_three(monkeypatch):
    """Home carousel caps at HOME_PREVIEW_MAX (=3) — never filler."""
    proposals = [
        {
            "id": f"p-{i}",
            "action_type": "gmail.send_email",
            "preview": {"summary": f"item {i}"},
            "source_message_id": f"m-{i}",
            "created_at": f"2026-09-14T12:0{i}:00Z",
        }
        for i in range(5)
    ]
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: proposals,
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    rows = brief_service.home_preview_rows("u1")
    assert len(rows) == 3


def test_home_preview_rows_two_matters_renders_two(monkeypatch):
    """When only 2 legitimate matters exist, Home renders 2 — never
    padded to 3."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [
            {
                "id": "p-a",
                "action_type": "gmail.send_email",
                "preview": {"summary": "reply to Sarah"},
                "source_message_id": "m-a",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [
            {
                "id": "n-a",
                "kind": "manager_alert",
                "title": "@nike asked for rates",
                "source_provider": "instagram",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "high",
                "created_at": "2026-09-14T11:00:00Z",
            }
        ],
    )
    rows = brief_service.home_preview_rows("u1")
    assert len(rows) == 2


def test_home_preview_rows_zero_matters_returns_empty(monkeypatch):
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
    assert brief_service.home_preview_rows("u1") == []


def test_home_preview_rows_use_source_slots(monkeypatch):
    """Home carousel icons switch on new source slots: gmail /
    instagram / calendar / babyg. Old ``performance`` / ``opportunity``
    / ``recap`` slots are gone from this data source."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [
            {
                "id": "p-gmail",
                "action_type": "gmail.send_email",
                "preview": {"summary": "reply ready"},
                "source_message_id": "m-gmail",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [
            {
                "id": "n-ig",
                "kind": "manager_alert",
                "title": "@nike asked for rates",
                "source_provider": "instagram",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            }
        ],
    )
    rows = brief_service.home_preview_rows("u1")
    slots = {r["slot"] for r in rows}
    assert slots.issubset({"gmail", "instagram", "calendar", "babyg"})
    for r in rows:
        assert r["slot"] not in {"performance", "opportunity", "recap"}


def test_home_preview_rows_link_at_brief_page(monkeypatch):
    """Every Home preview row links to the dedicated Brief page —
    Home is the compressed preview, `/creator/brief` is the deeper
    view."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [
            {
                "id": "p",
                "action_type": "gmail.send_email",
                "preview": {"summary": "x"},
                "source_message_id": "m",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    rows = brief_service.home_preview_rows("u1")
    assert all(r["href"] == "/creator/brief" for r in rows)


def test_dashboard_view_all_still_targets_brief_page() -> None:
    """Locked from Pass 1 — the view-all link must remain
    /creator/brief after the Pass 2 changes."""
    tpl = DASHBOARD_TEMPLATE.read_text()
    assert 'class="hv5-head-link" href="/creator/brief">view all' in tpl


# ---------------------------------------------------------------------------
# Lifecycle correction (Pass 2 §2)
# ---------------------------------------------------------------------------


def test_proposal_confirmed_status_maps_to_in_progress(monkeypatch):
    """Real business action state moves the item to in_progress —
    NOT is_read. A ``confirmed`` action proposal is the executor
    warming up; ``executing`` is the executor mid-flight. Both
    truthfully mean "babyg has started this and is waiting"."""
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [
            {
                "id": "p-conf",
                "action_type": "gmail.send_email",
                "preview": {"summary": "queued"},
                "source_message_id": "m",
                "status": "confirmed",
                "created_at": "2026-09-14T12:00:00Z",
            },
            {
                "id": "p-exec",
                "action_type": "gmail.send_email",
                "preview": {"summary": "sending"},
                "source_message_id": "m2",
                "status": "executing",
                "created_at": "2026-09-14T12:01:00Z",
            },
        ],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [],
    )
    view = brief_service.build_brief("u1")
    assert view["needs_you"] == []
    states = sorted(it["state"] for it in view["in_progress"])
    assert states == ["in_progress", "in_progress"]


def test_notification_is_read_never_produces_in_progress(monkeypatch):
    """Any combination of is_read on notification rows still yields
    only needs_you items. No notification alone can move to
    in_progress — that requires an action_proposals row."""
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
                "id": "n-1",
                "kind": "manager_alert",
                "title": "unseen",
                "source_provider": "instagram",
                "link_path": "/x",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T12:00:00Z",
            },
            {
                "id": "n-2",
                "kind": "manager_alert",
                "title": "seen",
                "source_provider": "instagram",
                "link_path": "/x",
                "is_read": True,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            },
        ],
    )
    view = brief_service.build_brief("u1")
    assert view["in_progress"] == []
    assert len(view["needs_you"]) == 2


def test_seen_flag_is_independent_of_business_state(monkeypatch):
    """``seen`` reflects whether the creator has opened the row.
    ``state`` reflects the business action state. They are two
    axes and never collapsed."""
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
                "id": "seen-still-needs",
                "kind": "manager_alert",
                "title": "read but unresolved",
                "source_provider": "instagram",
                "link_path": "/x",
                "is_read": True,
                "priority": "normal",
                "created_at": "2026-09-14T12:00:00Z",
            }
        ],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    assert item["seen"] is True
    assert item["state"] == "needs_you"


# ---------------------------------------------------------------------------
# Business-matter grouping (Pass 2 §3)
# ---------------------------------------------------------------------------


def test_multiple_events_same_thread_collapse_to_one_matter(monkeypatch):
    """Three IG events on the SAME source_thread_id collapse to ONE
    Brief item. The newest event donates the current summary; the
    priority is promoted to the strongest present."""
    thread_id = "b3a5ffff-0000-0000-0000-000000000042"
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
                "id": "n-1",
                "kind": "manager_alert",
                "title": "@nike asked for rates",
                "source_provider": "instagram",
                "source_thread_id": thread_id,
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T10:00:00Z",
            },
            {
                "id": "n-2",
                "kind": "manager_alert",
                "title": "@nike shared a $4,000 budget",
                "source_provider": "instagram",
                "source_thread_id": thread_id,
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "high",
                "created_at": "2026-09-14T11:00:00Z",
            },
            {
                "id": "n-3",
                "kind": "manager_alert",
                "title": "@nike wants 30-day usage",
                "source_provider": "instagram",
                "source_thread_id": thread_id,
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "urgent",
                "created_at": "2026-09-14T12:00:00Z",
            },
        ],
    )
    view = brief_service.build_brief("u1")
    assert len(view["needs_you"]) == 1
    item = view["needs_you"][0]
    # Newest event donates the current summary.
    assert "30-day usage" in item["what_happened"]
    # Priority is promoted to the strongest present.
    assert item["priority"] == "urgent"
    # Group carries the truthful event count.
    assert item["matter_event_count"] == 3


def test_unrelated_threads_same_sender_stay_separate(monkeypatch):
    """Two different source_thread_id values from what could be the
    same handle are TWO business matters — never conflated by
    sender alone."""
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
                "id": "n-a",
                "kind": "manager_alert",
                "title": "campaign A inquiry",
                "source_provider": "instagram",
                "source_thread_id": "thread-a",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T12:00:00Z",
            },
            {
                "id": "n-b",
                "kind": "manager_alert",
                "title": "campaign B inquiry",
                "source_provider": "instagram",
                "source_thread_id": "thread-b",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            },
        ],
    )
    view = brief_service.build_brief("u1")
    assert len(view["needs_you"]) == 2


def test_grouping_falls_back_to_row_id_when_no_thread_identity(monkeypatch):
    """A notification without source_thread_id or underlying
    identity still renders — grouping falls back to the Brief row
    id and never accidentally merges unrelated matters."""
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
                "id": "n-loose-1",
                "kind": "manager_alert",
                "title": "one-off inquiry",
                "source_provider": "instagram",
                "link_path": "/x",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T12:00:00Z",
            },
            {
                "id": "n-loose-2",
                "kind": "manager_alert",
                "title": "another one-off",
                "source_provider": "instagram",
                "link_path": "/x",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            },
        ],
    )
    view = brief_service.build_brief("u1")
    assert len(view["needs_you"]) == 2


def test_grouping_preserves_correct_source_and_actions(monkeypatch):
    """Grouped matter still carries the correct source label and
    still exposes the correct action set — Instagram STILL never
    gets a send action."""
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
                "id": "n-1",
                "kind": "manager_alert",
                "title": "hi",
                "source_provider": "instagram",
                "source_thread_id": "t-42",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T10:00:00Z",
            },
            {
                "id": "n-2",
                "kind": "manager_alert",
                "title": "budget update",
                "source_provider": "instagram",
                "source_thread_id": "t-42",
                "link_path": "/creator/instagram/dms",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            },
        ],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    assert item["source"] == "instagram"
    labels = [a["label"] for a in item["actions"]]
    assert "send reply" not in labels
    assert "review inquiry" in labels


def test_grouping_preserves_valid_ask_babyg_and_primary_action(monkeypatch):
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
                "id": "n-old",
                "kind": "new_dm",
                "title": "@nike asked for rates",
                "body": None,
                "source_provider": "instagram",
                "source_event_id": "instagram:message:old",
                "source_thread_id": "ig-thread-1",
                "underlying_type": "instagram_dm_message",
                "underlying_id": "msg-old",
                "link_path": "/creator/instagram/dms?thread=ig-thread-1",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T10:00:00Z",
            },
            {
                "id": "n-new",
                "kind": "new_dm",
                "title": "@nike shared a $4,000 budget",
                "body": "Ask for usage before quoting.",
                "source_provider": "instagram",
                "source_event_id": "instagram:message:new",
                "source_thread_id": "ig-thread-1",
                "underlying_type": "instagram_dm_message",
                "underlying_id": "msg-new",
                "link_path": "/creator/instagram/dms?thread=ig-thread-1",
                "is_read": False,
                "priority": "high",
                "created_at": "2026-09-14T12:00:00Z",
            },
        ],
    )
    item = brief_service.build_brief("u1")["needs_you"][0]
    assert item["what_happened"] == "@nike shared a $4,000 budget"
    endpoints = {a["label"]: a["endpoint"] for a in item["actions"]}
    assert endpoints["review inquiry"] == "/creator/instagram/dms?thread=ig-thread-1"
    assert endpoints["ask babyg"] == "/creator/bot?brief=notif:n-new"


def test_duplicate_native_connection_ids_collapse(monkeypatch):
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
                "id": "n-conn-old",
                "kind": "connection_request",
                "title": "Someone wants to connect.",
                "metadata": {"requester_name": "Jordan"},
                "underlying_type": "network_connection",
                "underlying_id": "conn-1",
                "link_path": "/creator/connections",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T10:00:00Z",
            },
            {
                "id": "n-conn-new",
                "kind": "connection_request",
                "title": "Someone wants to connect.",
                "metadata": {"requester_name": "Jordan"},
                "underlying_type": "network_connection",
                "underlying_id": "conn-1",
                "link_path": "/creator/connections",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            },
        ],
    )
    items = brief_service.build_brief("u1")["needs_you"]
    assert len(items) == 1
    assert items[0]["matter_event_count"] == 2


def test_distinct_native_connection_ids_stay_separate(monkeypatch):
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
                "id": "n-conn-a",
                "kind": "connection_request",
                "title": "New connection request",
                "underlying_type": "network_connection",
                "underlying_id": "conn-a",
                "link_path": "/creator/connections",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T10:00:00Z",
            },
            {
                "id": "n-conn-b",
                "kind": "connection_request",
                "title": "New connection request",
                "underlying_type": "network_connection",
                "underlying_id": "conn-b",
                "link_path": "/creator/connections",
                "is_read": False,
                "priority": "normal",
                "created_at": "2026-09-14T11:00:00Z",
            },
        ],
    )
    assert len(brief_service.build_brief("u1")["needs_you"]) == 2


# ---------------------------------------------------------------------------
# Manager context strip (Pass 2 §4)
# ---------------------------------------------------------------------------


def test_bot_template_ships_context_strip_conditional() -> None:
    """The context strip renders ONLY when brief_context is set on
    the template. The bot template still ships a compact placeholder
    that is scoped by the `{% if brief_context %}` guard so /creator/bot
    opened normally shows no strip."""
    tpl = (REPO / "app" / "templates" / "creator" / "bot.html").read_text()
    assert 'class="bot-brief-context"' in tpl
    assert 'data-bot-brief-context' in tpl
    assert "{% if brief_context %}" in tpl


def test_bot_template_context_strip_does_not_dump_body() -> None:
    """The strip surfaces only the compact source label + summary.
    The full recommendation, raw message body, and any provider
    payload MUST NOT be rendered — the strip is a one-line topic
    label."""
    tpl = (REPO / "app" / "templates" / "creator" / "bot.html").read_text()
    strip_block = tpl.split('class="bot-brief-context"', 1)[1].split("</div>", 1)[0]
    assert "recommendation" not in strip_block
    # The strip renders summary, source_label, and an icon — nothing else.
    assert "brief_context.summary" in strip_block
    assert "brief_context.source_label" in strip_block


def test_bot_context_strip_css_is_scoped_and_present() -> None:
    css = APP_CSS.read_text()
    assert ".bot-brief-context {" in css
    assert ".bot-brief-context-source {" in css
    assert ".bot-brief-context-summary {" in css
    # Mobile-scoped tweaks exist.
    mobile_blocks = css.split("@media (max-width: 767px)")
    assert any(".bot-brief-context {" in blk for blk in mobile_blocks[1:])


# ---------------------------------------------------------------------------
# Existing safeguards re-locked after Pass 2 edits.
# ---------------------------------------------------------------------------


def test_no_authenticated_document_prefetch_after_pass2() -> None:
    dashboard = DASHBOARD_TEMPLATE.read_text()
    bot_tpl = (REPO / "app" / "templates" / "creator" / "bot.html").read_text()
    brief_tpl = BRIEF_TEMPLATE.read_text()
    for path in ("/creator/brief", "/creator/bot", "/creator/discover"):
        assert f'rel="prefetch" href="{path}"' not in dashboard
        assert f'rel="prefetch" href="{path}"' not in bot_tpl
        assert f'rel="prefetch" href="{path}"' not in brief_tpl


def test_pass2_bot_prompts_still_max_four_and_instagram_never_sends() -> None:
    """Regression re-lock — Pass 1 chip contract survives Pass 2."""
    ig_chips = bot_prompts_module.compute_prompts(
        brief_context={"source": "instagram", "summary": "@nike"}
    )
    assert 1 <= len(ig_chips) <= 4
    for chip in ig_chips:
        assert "send" not in chip["text"].lower() or "email" in chip["text"].lower()

    gmail_chips = bot_prompts_module.compute_prompts(
        brief_context={"source": "gmail", "summary": "acme"}
    )
    assert 1 <= len(gmail_chips) <= 4


# ---------------------------------------------------------------------------
# Pass 3 — restore real Gmail + Instagram intelligence
#
# The spec locks four gaps that Pass 2 left open:
#   1. Instagram DMs only reach Brief when priority is high/urgent.
#   2. Gmail sweep-generated proposals expose a factual `preview.title`
#      heading, and the `gmail.create_draft` action_type is send-eligible.
#   3. Platform casing is `Instagram`, `Gmail`, `Calendar` (Title-cased)
#      and `babyg` (always lowercase). No CSS uppercases these labels.
#   4. Unknown/unresolvable sources are EXCLUDED — never rebranded as
#      babyg — and fallback icons render the babyg mark, never a
#      generic clock SVG.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("priority", "expected_empty"),
    [
        ("normal", True),   # casual DM → excluded
        ("low", True),      # casual DM → excluded
        (None, True),       # missing priority defaults to normal → excluded
        ("high", False),    # collab/deal keyword → surfaced
        ("urgent", False),  # explicit urgent → surfaced
    ],
)
def test_instagram_new_dm_priority_filter(monkeypatch, priority, expected_empty):
    """The Instagram DM ingest layer sets ``priority='high'`` for
    collab/deal keywords or reel/post attachments; everything else
    stays ``normal``. The Brief consumer path must respect that
    signal: only high/urgent reaches the Brief. Casual chatter is
    not a business matter and MUST NOT show up."""
    row = {
        "id": "notif-ig-priority",
        "kind": "new_dm",
        "title": "@nike sent a message",
        "body": None,
        "source_provider": "instagram",
        "source_thread_id": "ig-thread-priority",
        "underlying_type": "instagram_dm_message",
        "underlying_id": "msg-priority",
        "link_path": "/creator/instagram/dms?thread=ig-thread-priority",
        "is_read": False,
        "created_at": "2026-09-14T12:00:00Z",
    }
    if priority is not None:
        row["priority"] = priority
    monkeypatch.setattr(
        action_proposals_module,
        "list_pending_for_user",
        lambda *, user_id, limit=10: [],
    )
    monkeypatch.setattr(
        notifications_module,
        "list_for_user",
        lambda user_id, *, limit=50, include_archived=False: [row],
    )
    view = brief_service.build_brief("u1")
    assert view["empty"] is expected_empty


def test_gmail_sweep_proposal_uses_preview_title_as_heading(monkeypatch):
    """`sweep_gmail_briefs` stashes a factual title in
    ``preview.title`` (e.g. "draft reply to Acme about the Q4
    campaign") — the Brief must prefer it over the less specific
    ``summary``/``subject`` fields."""
    proposal = {
        "id": "prop-sweep-1",
        "action_type": "gmail.create_draft",
        "provider": "google",
        "preview": {
            "title": "draft reply to Acme about the Q4 campaign",
            "summary": "acme q4",
            "subject": "Q4 campaign",
            "to": "acme@example.com",
            "body": "Sure, happy to.",
            "thread_id": "thr-42",
        },
        "source_message_id": "bot-msg-sweep-1",
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
    assert item["what_happened"] == "draft reply to Acme about the Q4 campaign"


def test_gmail_create_draft_action_type_is_send_eligible(monkeypatch):
    """`gmail.create_draft` is the action_type Gmail's sweep produces.
    It must land on the Brief with a real send/draft action pointing
    at the existing bot confirm endpoint — the manager chat is still
    the confirmation surface."""
    proposal = {
        "id": "prop-create-draft",
        "action_type": "gmail.create_draft",
        "provider": "google",
        "preview": {"title": "draft reply to Acme"},
        "source_message_id": "bot-msg-cd",
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
    labels = {a["label"]: a for a in item["actions"]}
    # `gmail.create_draft` labels as "draft reply" — never claim a
    # send when the executor only prepares a draft.
    assert "draft reply" in labels
    assert labels["draft reply"]["endpoint"] == "/creator/bot/actions/bot-msg-cd/confirm"
    assert labels["draft reply"]["method"] == "POST"


def test_gmail_sweep_proposal_without_source_message_id_hides_send_action(
    monkeypatch,
):
    """Autonomous sweep proposals that persist WITHOUT a
    ``source_message_id`` cannot use the bot confirm endpoint — the
    Brief must fall back to ``ask babyg`` only. The manager chat is
    the confirmation surface for autonomous proposals per the
    manager-architecture freeze."""
    proposal = {
        "id": "prop-sweep-noid",
        "action_type": "gmail.create_draft",
        "provider": "google",
        "preview": {"title": "draft reply to Acme"},
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
    assert "draft reply" not in labels
    assert "send reply" not in labels
    assert labels == ["ask babyg"]


def test_gmail_proposal_with_no_persisted_heading_is_excluded(monkeypatch):
    """No factual heading available (no title/summary/brief/subject)
    → item is EXCLUDED. No fake fallback text like "a new gmail
    thread needs a decision" may appear — spec §NO fake/fallback
    matters."""
    proposal = {
        "id": "prop-nofact",
        "action_type": "gmail.send_email",
        "provider": "google",
        "preview": {},
        "source_message_id": "bot-msg-x",
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
    assert view["empty"] is True


def test_no_fake_fallback_strings_in_source_module() -> None:
    """The known-bad fake fallback strings from Pass 2 are gone
    from the brief service source. Grepping the module is the
    cheapest way to keep them out — a well-meaning refactor
    that reintroduces them is caught before deploy."""
    src = (REPO / "app" / "services" / "brief.py").read_text()
    for banned in (
        "a new gmail thread needs a decision",
        "a new instagram inquiry needs a look",
        "a calendar update needs attention",
        "a babyg update needs review",
        "an update is waiting",
    ):
        assert banned not in src, f"forbidden fake fallback still present: {banned!r}"


def test_platform_casing_is_locked() -> None:
    """The four platform labels are locked at the source level.
    babyg is ALWAYS lowercase; every other platform is Title-cased.
    Any change to this rule breaks the product spec."""
    assert brief_service._source_label("instagram") == "Instagram"
    assert brief_service._source_label("gmail") == "Gmail"
    assert brief_service._source_label("calendar") == "Calendar"
    assert brief_service._source_label("babyg") == "babyg"
    # ``system`` maps to babyg (still lowercase).
    assert brief_service._source_label("system") == "babyg"


def test_platform_label_css_does_not_uppercase() -> None:
    """`.brief-item-source-label` and `.bot-brief-context-source`
    MUST NOT carry ``text-transform: uppercase`` — the visual
    casing IS the product spec, delivered by ``_source_label``."""
    import re
    css = APP_CSS.read_text()
    for cls in (".brief-item-source-label", ".bot-brief-context-source"):
        block = css.split(cls + " {", 1)[1].split("}", 1)[0]
        # Strip /* ... */ comments before scanning declarations —
        # the rule may legally carry a comment that names the
        # banned declaration.
        stripped = re.sub(r"/\*.*?\*/", "", block, flags=re.DOTALL)
        assert "text-transform: uppercase" not in stripped, (
            f"{cls} must not use text-transform: uppercase — see Pass 3"
        )


def test_brief_template_fallback_icon_is_babyg_mark() -> None:
    """The unknown-source branch in the brief page renders the
    babyg mark image, never a clock/generic SVG. Unknown sources
    are excluded upstream so this branch normally does not fire —
    but if it does, the fallback is the brand mark."""
    tpl = BRIEF_TEMPLATE.read_text()
    fallback_block = tpl.split("{%- else -%}", 1)[1].split("{%- endif -%}", 1)[0]
    assert "logo-bg.png" in fallback_block
    # No stray clock/generic SVG lands in the fallback.
    assert "<svg" not in fallback_block


def test_bot_context_strip_fallback_icon_is_babyg_mark() -> None:
    """Same rule for the manager context strip on /creator/bot —
    fallback icon is the babyg mark, not a clock SVG."""
    tpl = (REPO / "app" / "templates" / "creator" / "bot.html").read_text()
    strip_block = tpl.split("bot-brief-context-icon", 1)[1].split("</span>", 1)[0]
    else_block = strip_block.split("{% else %}", 1)[1]
    assert "logo-bg.png" in else_block
    # The fallback branch must not smuggle in another SVG.
    assert "<svg" not in else_block


def test_dashboard_home_brief_fallback_icon_is_babyg_mark() -> None:
    """Home Brief carousel row's fallback icon is also the babyg
    mark — no clock/generic SVG in the else branch of the source
    switch."""
    tpl = DASHBOARD_TEMPLATE.read_text()
    icon_block = tpl.split('class="hv5-brief-icon"', 1)[1].split("</span>", 1)[0]
    else_block = icon_block.split("{% else %}", 1)[1]
    assert "logo-bg.png" in else_block
    assert "<svg" not in else_block
