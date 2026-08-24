"""Awareness snapshot service tests.

The snapshot is the source of truth for the composer chip strip AND
the system-prompt state block. It must:

  * never raise — every signal reader wraps in try/except
  * cache per user for ~30s
  * respect the ``force`` flag to bypass the cache
  * fall back to an empty snapshot for unknown users
  * project only safe fields (peer name yes, exact coords no)
  * render human-readable summary lines that drop empty signals
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.services import babyg_awareness


@pytest.fixture(autouse=True)
def _clear_cache():
    """Every test starts with a fresh snapshot cache."""
    babyg_awareness._CACHE.clear()
    yield
    babyg_awareness._CACHE.clear()


class _StubClient:
    """Minimal service-client stub for the raw-SQL readers (action
    proposals, deal stages)."""

    def __init__(self, tables: dict[str, list[dict[str, Any]]] | None = None) -> None:
        self.tables = tables or {}

    def table(self, name: str):
        rows = list(self.tables.get(name, []))
        return _StubTable(rows)


class _StubTable:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def select(self, *_a, **_k):
        return self

    def eq(self, col: str, val: Any):
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, _n: int):
        return self

    class _Not:
        def __init__(self, parent: _StubTable) -> None:
            self._parent = parent

        def is_(self, col: str, val: str):
            if val == "null":
                self._parent._rows = [
                    r for r in self._parent._rows if r.get(col) is not None
                ]
            return self._parent

    @property
    def not_(self):
        return _StubTable._Not(self)

    def execute(self):
        return SimpleNamespace(data=list(self._rows))


# ---------------------------------------------------------------------------
# empty / defensive shape
# ---------------------------------------------------------------------------


def test_empty_snapshot_for_blank_user_id() -> None:
    snap = babyg_awareness.snapshot("")
    assert snap["unread_dms"] == {"count": 0, "latest_peer_name": None}
    assert snap["recent_connection_accepted"] is None
    assert snap["next_booking"] is None
    assert snap["pending_action_proposal"] is None


def test_snapshot_caches_per_user(monkeypatch) -> None:
    """A second call within TTL must NOT rebuild."""
    calls = {"n": 0}

    def _fake_build(uid: str) -> dict[str, Any]:
        calls["n"] += 1
        return {"marker": uid, **babyg_awareness._empty()}

    monkeypatch.setattr(babyg_awareness, "_build", _fake_build)
    a1 = babyg_awareness.snapshot("u1")
    a2 = babyg_awareness.snapshot("u1")
    assert a1 is a2
    assert calls["n"] == 1


def test_snapshot_force_rebuilds(monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(
        babyg_awareness,
        "_build",
        lambda uid: (calls.__setitem__("n", calls["n"] + 1), babyg_awareness._empty())[1],
    )
    babyg_awareness.snapshot("u1")
    babyg_awareness.snapshot("u1", force=True)
    assert calls["n"] == 2


def test_invalidate_drops_the_cached_snapshot(monkeypatch) -> None:
    calls = {"n": 0}
    monkeypatch.setattr(
        babyg_awareness,
        "_build",
        lambda uid: (calls.__setitem__("n", calls["n"] + 1), babyg_awareness._empty())[1],
    )
    babyg_awareness.snapshot("u1")
    babyg_awareness.invalidate("u1")
    babyg_awareness.snapshot("u1")
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# signal readers — safe to fail
# ---------------------------------------------------------------------------


def test_unread_dms_gracefully_degrades_when_dms_missing(monkeypatch) -> None:
    """If dms module explodes, snapshot still returns 0 unread."""
    from app.services import dms as dms_module

    monkeypatch.setattr(
        dms_module, "unread_count_for_user", lambda uid: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    monkeypatch.setattr(
        dms_module, "list_threads_for_user", lambda uid: (_ for _ in ()).throw(RuntimeError("boom"))
    )
    sig = babyg_awareness._unread_dms("u1")
    assert sig == {"count": 0, "latest_peer_name": None}


def test_unread_dms_projects_peer_name_only(monkeypatch) -> None:
    from app.services import dms as dms_module
    from app.services import profiles as profiles_module

    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 3)
    monkeypatch.setattr(
        dms_module,
        "list_threads_for_user",
        lambda uid: [{"peer_id": "peer-42"}],
    )
    monkeypatch.setattr(
        profiles_module,
        "get_creator_profile",
        lambda uid: {
            "user_id": uid,
            "full_name": "Maya Chen",
            # Owner-private fields MUST NOT leak into the snapshot.
            "location_lat": 34.0522,
            "baseline_followers": 42_000,
            "tier": "pro",
        },
    )
    sig = babyg_awareness._unread_dms("u1")
    assert sig["count"] == 3
    assert sig["latest_peer_name"] == "Maya Chen"
    # Snapshot must never carry private fields.
    for private in ("location_lat", "baseline_followers", "tier"):
        assert private not in sig


def test_next_booking_returns_minutes_until_start(monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services import bookings as bookings_module

    starts = (datetime.now(UTC) + timedelta(minutes=40)).isoformat()
    monkeypatch.setattr(
        bookings_module,
        "list_for_user",
        lambda uid, **kw: [
            {
                "id": "b-1",
                "title": "Studio House shoot",
                "starts_at": starts,
                "status": "confirmed",
                "venue_name": "Silverlake",
            }
        ],
    )
    sig = babyg_awareness._next_booking("u1")
    assert sig is not None
    assert sig["title"] == "Studio House shoot"
    assert 35 <= sig["minutes_until_start"] <= 45
    assert sig["venue_name"] == "Silverlake"


def test_next_booking_skips_cancelled(monkeypatch) -> None:
    from datetime import UTC, datetime, timedelta

    from app.services import bookings as bookings_module

    starts = (datetime.now(UTC) + timedelta(minutes=30)).isoformat()
    monkeypatch.setattr(
        bookings_module,
        "list_for_user",
        lambda uid, **kw: [
            {"id": "b-x", "title": "cancelled", "starts_at": starts, "status": "cancelled"},
        ],
    )
    assert babyg_awareness._next_booking("u1") is None


def test_pending_action_proposal_reads_via_service_client(monkeypatch) -> None:
    from app.core import supabase_client

    client = _StubClient({
        "action_proposals": [
            {
                "id": "ap-1",
                "user_id": "u1",
                "action_type": "gmail.send_email",
                "action_category": "external_write",
                "status": "pending",
            }
        ]
    })
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: client)
    sig = babyg_awareness._pending_action_proposal("u1")
    assert sig == {
        "id": "ap-1",
        "action_type": "gmail.send_email",
        "action_category": "external_write",
    }


# ---------------------------------------------------------------------------
# summary lines — dropping empty signals + rendering the non-empty ones
# ---------------------------------------------------------------------------


def test_summary_lines_drop_empty_signals() -> None:
    lines = babyg_awareness.snapshot_summary_lines(babyg_awareness._empty())
    assert lines == []


def test_summary_lines_render_populated_signals() -> None:
    snap = babyg_awareness._empty()
    snap["unread_dms"] = {"count": 3, "latest_peer_name": "Maya Chen"}
    snap["recent_connection_accepted"] = {
        "peer_id": "p1",
        "peer_name": "Garrett Reynolds",
        "peer_handle": "garrett",
    }
    snap["next_booking"] = {
        "id": "b-1",
        "title": "Studio House shoot",
        "starts_at": "2026-06-19T15:00:00Z",
        "minutes_until_start": 40,
        "venue_name": "Silverlake",
    }
    snap["pending_action_proposal"] = {
        "id": "ap-1",
        "action_type": "gmail.send_email",
        "action_category": "external_write",
    }
    snap["recent_hot_drop"] = {
        "id": "h-1",
        "title": "Miami rooftop pop-up",
        "category": "venue",
    }
    lines = babyg_awareness.snapshot_summary_lines(snap)
    joined = "\n".join(lines)
    assert "3 unread DM(s)" in joined
    assert "Maya Chen" in joined
    assert "Garrett Reynolds just accepted your connection" in joined
    assert "Studio House shoot" in joined
    assert "gmail.send_email" in joined
    assert "Miami rooftop pop-up" in joined
