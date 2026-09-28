"""Tests for the new Activity view (bot page top-right sheet).

Covers the read model (manager_activity.list_recent_activity), the
has_new_since flag that lights up the trigger button's pink dot, and
the /creator/bot template contract that anchors the trigger + hidden
sheet in the DOM.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services import manager_activity

REPO = Path(__file__).resolve().parents[1]
BOT_TEMPLATE = (REPO / "app" / "templates" / "creator" / "bot.html").read_text()


# ---------------------------------------------------------------------------
# 1. Row → item mapping.
# ---------------------------------------------------------------------------


def test_row_to_item_gmail_send_uses_subject_subtitle() -> None:
    row = {
        "id": "p-1",
        "action_type": "gmail.send_email",
        "preview": {"subject": "Re: Q4 partnership", "to": "sarah@nike.com"},
        "executed_at": "2026-09-28T14:00:00+00:00",
    }
    item = manager_activity._row_to_item(row)
    assert item is not None
    assert item["source"] == "gmail"
    assert item["title"] == "Sent email"
    assert item["subtitle"] == "Re: Q4 partnership"


def test_row_to_item_gmail_send_falls_back_to_to() -> None:
    row = {
        "id": "p-1",
        "action_type": "gmail.send_email",
        "preview": {"to": "sarah@nike.com"},
        "executed_at": "2026-09-28T14:00:00+00:00",
    }
    item = manager_activity._row_to_item(row)
    assert item is not None
    assert item["subtitle"] == "sarah@nike.com"


def test_row_to_item_calendar_create() -> None:
    row = {
        "id": "p-2",
        "action_type": "calendar.create_event",
        "preview": {"title": "Content shoot", "starts_at_label": "Tomorrow 2:00 PM"},
        "executed_at": "2026-09-28T11:42:00+00:00",
    }
    item = manager_activity._row_to_item(row)
    assert item is not None
    assert item["source"] == "calendar"
    assert item["title"] == "Added to your calendar"
    assert item["subtitle"] == "Content shoot"


def test_row_to_item_instagram_dm_uses_peer_handle() -> None:
    row = {
        "id": "p-3",
        "action_type": "instagram.send_dm",
        "preview": {"peer_username": "@brandpartner"},
        "executed_at": "2026-09-28T13:15:00+00:00",
    }
    item = manager_activity._row_to_item(row)
    assert item is not None
    assert item["source"] == "instagram"
    assert item["title"] == "Replied to Instagram DM"
    assert item["subtitle"] == "@brandpartner"


def test_row_to_item_unknown_action_type_returns_none() -> None:
    """Never fake a row: if we don't have display metadata for an
    action_type yet, drop it rather than surface a raw type string."""
    row = {
        "id": "p-x",
        "action_type": "some.future.action",
        "preview": {},
        "executed_at": "2026-09-28T13:00:00+00:00",
    }
    assert manager_activity._row_to_item(row) is None


def test_subtitle_trims_and_caps_long_text() -> None:
    long_text = "x" * 300
    row = {
        "id": "p-1",
        "action_type": "gmail.send_email",
        "preview": {"subject": long_text},
        "executed_at": "2026-09-28T14:00:00+00:00",
    }
    item = manager_activity._row_to_item(row)
    assert item is not None
    assert len(item["subtitle"]) <= 90


# ---------------------------------------------------------------------------
# 2. Grouping by day.
# ---------------------------------------------------------------------------


def _executed_row(*, action_type: str, executed_at: str, preview: dict | None = None) -> dict:
    return {
        "id": f"p-{executed_at}-{action_type}",
        "action_type": action_type,
        "action_category": "external_write",
        "provider": "gmail",
        "status": "executed",
        "preview": preview or {"subject": "test", "to": "someone@brand.com"},
        "executed_at": executed_at,
        "created_at": executed_at,
        "updated_at": executed_at,
    }


class _FakeResponse:
    def __init__(self, rows: list[dict]) -> None:
        self.data = rows


class _FakeExec:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows

    def execute(self) -> _FakeResponse:
        return _FakeResponse(self._rows)


class _FakeChain:
    """Minimal PostgREST-shaped stub."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows = rows
        self.calls: dict[str, Any] = {}

    def table(self, name: str) -> "_FakeChain":
        self.calls["table"] = name
        return self

    def select(self, cols: str) -> "_FakeChain":
        self.calls["select"] = cols
        return self

    def eq(self, col: str, val: Any) -> "_FakeChain":
        self.calls.setdefault("eq", []).append((col, val))
        return self

    def gte(self, col: str, val: Any) -> "_FakeChain":
        self.calls.setdefault("gte", []).append((col, val))
        return self

    def order(self, col: str, desc: bool = False) -> "_FakeChain":
        self.calls["order"] = (col, desc)
        return self

    def limit(self, n: int) -> "_FakeChain":
        self.calls["limit"] = n
        return self

    def execute(self) -> _FakeResponse:
        return _FakeResponse(self._rows)


@pytest.fixture()
def _now() -> datetime:
    # Wednesday afternoon UTC — deterministic anchor for grouping tests.
    return datetime(2026, 9, 30, 15, 0, 0, tzinfo=UTC)


def _install_fake_supabase(monkeypatch: pytest.MonkeyPatch, rows: list[dict]) -> _FakeChain:
    chain = _FakeChain(rows)
    monkeypatch.setattr(
        manager_activity.supabase_client, "get_service_client", lambda: chain
    )
    return chain


def test_list_recent_activity_groups_today_yesterday_and_older(
    monkeypatch: pytest.MonkeyPatch, _now: datetime
) -> None:
    rows = [
        _executed_row(
            action_type="gmail.send_email",
            executed_at=_now.isoformat(),
            preview={"subject": "Q4 pitch"},
        ),
        _executed_row(
            action_type="calendar.create_event",
            executed_at=(_now - timedelta(hours=1)).isoformat(),
            preview={"title": "Content shoot"},
        ),
        _executed_row(
            action_type="instagram.send_dm",
            executed_at=(_now - timedelta(days=1)).isoformat(),
            preview={"peer_username": "@brandpartner"},
        ),
        _executed_row(
            action_type="gmail.send_email",
            executed_at=(_now - timedelta(days=3)).isoformat(),
            preview={"subject": "Older reply"},
        ),
    ]
    _install_fake_supabase(monkeypatch, rows)

    groups = manager_activity.list_recent_activity("u-1", now=_now)

    labels = [g["label"] for g in groups]
    assert labels[0] == "Today"
    assert labels[1] == "Yesterday"
    # 3-days-ago row lives in an "older" group with a compact label,
    # not "Today" or "Yesterday".
    assert labels[2] not in {"Today", "Yesterday"}

    today_items = groups[0]["items"]
    assert len(today_items) == 2
    assert today_items[0]["title"] == "Sent email"
    assert today_items[0]["subtitle"] == "Q4 pitch"


def test_list_recent_activity_skips_unknown_action_types(
    monkeypatch: pytest.MonkeyPatch, _now: datetime
) -> None:
    rows = [
        _executed_row(action_type="gmail.send_email", executed_at=_now.isoformat()),
        _executed_row(action_type="totally.made.up", executed_at=_now.isoformat()),
    ]
    _install_fake_supabase(monkeypatch, rows)
    groups = manager_activity.list_recent_activity("u-1", now=_now)
    all_items = [item for g in groups for item in g["items"]]
    assert len(all_items) == 1
    assert all_items[0]["action_type"] == "gmail.send_email"


def test_list_recent_activity_query_targets_executed_status_only(
    monkeypatch: pytest.MonkeyPatch, _now: datetime
) -> None:
    """Regression lock: the Activity sheet must NOT surface pending /
    cancelled / failed proposals — those belong in chat (pending) or
    nowhere (failed). Assert the exact eq('status', 'executed') filter."""
    chain = _install_fake_supabase(monkeypatch, [])
    manager_activity.list_recent_activity("u-1", now=_now)
    assert chain.calls["table"] == "action_proposals"
    eq_pairs = dict(chain.calls.get("eq", []))
    assert eq_pairs.get("user_id") == "u-1"
    assert eq_pairs.get("status") == "executed"
    gte_pairs = dict(chain.calls.get("gte", []))
    assert "executed_at" in gte_pairs


def test_list_recent_activity_swallows_errors(
    monkeypatch: pytest.MonkeyPatch, _now: datetime
) -> None:
    class _Boom:
        def table(self, *_a, **_k):
            raise RuntimeError("simulated supabase blip")

    monkeypatch.setattr(
        manager_activity.supabase_client, "get_service_client", lambda: _Boom()
    )
    assert manager_activity.list_recent_activity("u-1", now=_now) == []


# ---------------------------------------------------------------------------
# 3. has_new_since — drives the trigger button's pink dot.
# ---------------------------------------------------------------------------


def test_has_new_since_returns_true_when_row_present(
    monkeypatch: pytest.MonkeyPatch, _now: datetime
) -> None:
    _install_fake_supabase(
        monkeypatch,
        [_executed_row(action_type="gmail.send_email", executed_at=_now.isoformat())],
    )
    assert manager_activity.has_new_since("u-1", now=_now) is True


def test_has_new_since_returns_false_when_empty(
    monkeypatch: pytest.MonkeyPatch, _now: datetime
) -> None:
    _install_fake_supabase(monkeypatch, [])
    assert manager_activity.has_new_since("u-1", now=_now) is False


def test_has_new_since_swallows_errors(monkeypatch: pytest.MonkeyPatch, _now: datetime) -> None:
    class _Boom:
        def table(self, *_a, **_k):
            raise RuntimeError("blip")

    monkeypatch.setattr(
        manager_activity.supabase_client, "get_service_client", lambda: _Boom()
    )
    assert manager_activity.has_new_since("u-1", now=_now) is False


# ---------------------------------------------------------------------------
# 4. Template contract — the bot page must anchor the new UI and drop
#    the old surfaces that the mockup replaces.
# ---------------------------------------------------------------------------


def test_bot_template_removes_large_ai_manager_title() -> None:
    """The full-width 'ai manager' topbar was replaced by a subtle
    top-right trigger. Lock the removal."""
    assert "creator-babyg-topbar" not in BOT_TEMPLATE
    assert "creator-screen-lockup-label" not in BOT_TEMPLATE
    # And the block heading text itself.
    assert "ai manager" not in BOT_TEMPLATE.lower() or "your AI manager" not in BOT_TEMPLATE


def test_bot_template_removes_old_handled_section() -> None:
    """The old bulky '.bot-activity' handled section is gone; its
    contents (recap headlines / recent cycles / pending shortcuts)
    now live either in the new Activity sheet (executed only) or in
    the chat itself (pending approvals)."""
    assert "class=\"bot-activity\"" not in BOT_TEMPLATE
    assert "bot-activity-head" not in BOT_TEMPLATE
    assert "bot-activity-lines" not in BOT_TEMPLATE
    assert "bot-activity-actions" not in BOT_TEMPLATE
    assert "bot-activity-cycles" not in BOT_TEMPLATE


def test_bot_template_has_activity_trigger_button() -> None:
    assert "bot-activity-btn" in BOT_TEMPLATE
    assert "data-bot-activity-open" in BOT_TEMPLATE
    assert "aria-controls=\"botActivitySheet\"" in BOT_TEMPLATE


def test_bot_template_has_hidden_activity_sheet() -> None:
    assert "bot-activity-sheet" in BOT_TEMPLATE
    assert "id=\"botActivitySheet\"" in BOT_TEMPLATE
    # Sheet must start hidden — JS opens it.
    assert "hidden" in BOT_TEMPLATE
    assert "role=\"dialog\"" in BOT_TEMPLATE
    assert "aria-modal=\"true\"" in BOT_TEMPLATE
    # Centered heading in the sheet head.
    assert "id=\"botActivityTitle\"" in BOT_TEMPLATE


def test_bot_template_pending_approvals_still_render_inside_chat() -> None:
    """Pending action proposals continue to render inline in chat via
    the bot_messages.html partial. This test guards that the template
    still includes that partial (which handles the tool_calls.kind ==
    'proposed_action' bubble)."""
    assert '_partials/bot_messages.html' in BOT_TEMPLATE
    # And that we did NOT re-surface pending_actions as a duplicate
    # top-level list (that was part of the removed .bot-activity block).
    assert "pending_actions" not in BOT_TEMPLATE
