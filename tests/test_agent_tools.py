"""Tests for the read-only agent tool facade."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import agent_tools


class _FakeSupabase:
    def __init__(self, rows_by_table=None, raise_on=None):
        self.rows_by_table = rows_by_table or {}
        self.raise_on = raise_on or set()

    def table(self, name):
        if name in self.raise_on:

            class _Boom:
                def __getattr__(self, _):
                    raise RuntimeError(f"supabase down ({name})")

            return _Boom()
        return _FakeTable(self.rows_by_table.get(name, []))


class _FakeTable:
    def __init__(self, rows):
        self._rows = rows

    def select(self, *_): return self
    def eq(self, *_): return self
    def in_(self, *_): return self
    def lte(self, *_): return self
    def order(self, *_, **__): return self
    def limit(self, *_): return self
    def execute(self):
        return _Result(list(self._rows))


class _Result:
    def __init__(self, data):
        self.data = data


def _install(monkeypatch, **kwargs) -> _FakeSupabase:
    fake = _FakeSupabase(**kwargs)
    monkeypatch.setattr(
        agent_tools.supabase_client, "get_service_client", lambda: fake
    )
    return fake


def test_stale_draft_candidates_returns_rows(monkeypatch) -> None:
    rows = [
        {"id": "d1", "creator_id": "c1", "status": "proposed", "updated_at": "2026-08-01T00:00:00Z"},
    ]
    _install(monkeypatch, rows_by_table={"babyg_memory_drafts": rows})
    result = agent_tools.stale_draft_candidates(
        "c1", now=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert result == rows


def test_stale_draft_candidates_swallows_error(monkeypatch) -> None:
    _install(monkeypatch, raise_on={"babyg_memory_drafts"})
    result = agent_tools.stale_draft_candidates(
        "c1", now=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert result == []


def test_ghosted_deal_candidates_returns_rows(monkeypatch) -> None:
    rows = [
        {"id": "deal1", "creator_id": "c1", "brand_name": "acme", "stage": "negotiating", "last_touch_at": "2026-08-15T00:00:00Z"},
    ]
    _install(monkeypatch, rows_by_table={"babyg_memory_deals": rows})
    assert agent_tools.ghosted_deal_candidates(
        "c1", now=datetime(2026, 9, 3, tzinfo=UTC)
    ) == rows


def test_upcoming_bookings_projects_slim(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.bookings,
        "list_for_user",
        lambda uid, horizon, limit: [
            {
                "id": "b1",
                "title": "brand call",
                "starts_at": "2026-09-04T15:00:00Z",
                "ends_at": "2026-09-04T15:30:00Z",
                "venue_name": None,
                "status": "confirmed",
                "internal_notes": "should not leak",  # not in slim
            }
        ],
    )
    result = agent_tools.upcoming_bookings("c1", limit=5)
    assert len(result) == 1
    assert "internal_notes" not in result[0]
    assert result[0]["title"] == "brand call"


def test_upcoming_bookings_swallows_error(monkeypatch) -> None:
    def _boom(*_, **__):
        raise RuntimeError("bookings down")

    monkeypatch.setattr(agent_tools.bookings, "list_for_user", _boom)
    assert agent_tools.upcoming_bookings("c1") == []


def test_unread_dms_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(
        agent_tools.dms, "unread_count_for_user", lambda uid: 4
    )
    assert agent_tools.unread_dms_snapshot("c1") == {"count": 4}


def test_unread_dms_snapshot_swallows_error(monkeypatch) -> None:
    def _boom(_):
        raise RuntimeError("dms down")

    monkeypatch.setattr(agent_tools.dms, "unread_count_for_user", _boom)
    assert agent_tools.unread_dms_snapshot("c1") == {"count": 0}


def test_pending_action_proposals_snapshot_buckets_by_kind(monkeypatch) -> None:
    rows = [
        {"id": "a1", "action_type": "gmail.create_draft"},
        {"id": "a2", "action_type": "gmail.create_draft"},
        {"id": "a3", "action_type": "babyg.create_booking"},
    ]
    monkeypatch.setattr(
        agent_tools.action_proposals,
        "list_pending_for_user",
        lambda **kwargs: rows,
    )
    result = agent_tools.pending_action_proposals_snapshot("c1")
    assert result["count"] == 3
    assert result["by_action_type"] == {
        "gmail.create_draft": 2,
        "babyg.create_booking": 1,
    }


def test_observe_returns_all_dimensions(monkeypatch) -> None:
    _install(monkeypatch, rows_by_table={
        "babyg_memory_drafts": [],
        "babyg_memory_deals": [],
    })
    monkeypatch.setattr(
        agent_tools.bookings, "list_for_user", lambda *a, **kw: []
    )
    monkeypatch.setattr(agent_tools.dms, "unread_count_for_user", lambda _: 0)
    monkeypatch.setattr(
        agent_tools.action_proposals,
        "list_pending_for_user",
        lambda **kwargs: [],
    )
    snap = agent_tools.observe("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    assert set(snap.keys()) == {
        "as_of",
        "stale_drafts",
        "ghosted_deals",
        "upcoming_bookings",
        "unread_dms",
        "pending_action_proposals",
    }


def test_delta_summary_zero_when_nothing_new() -> None:
    snap = {
        "stale_drafts": [],
        "ghosted_deals": [],
        "upcoming_bookings": [],
        "unread_dms": {"count": 0},
        "pending_action_proposals": {"count": 0},
    }
    delta = agent_tools.delta_summary(snap)
    assert sum(delta.values()) == 0


def test_delta_summary_counts_correctly() -> None:
    snap = {
        "stale_drafts": [1, 2],
        "ghosted_deals": [1],
        "upcoming_bookings": [],
        "unread_dms": {"count": 3},
        "pending_action_proposals": {"count": 4},
    }
    delta = agent_tools.delta_summary(snap)
    assert delta == {
        "stale_drafts": 2,
        "ghosted_deals": 1,
        "upcoming_bookings": 0,
        "unread_dms": 3,
        "pending_action_proposals": 4,
    }
