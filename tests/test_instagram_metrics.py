"""Instagram Phase C — daily snapshot + growth history.

Mocks Supabase at the service-client boundary so we can prove:
  - snapshot_daily upserts with the right shape
  - snapshot_daily skips when unconfigured / disconnected / empty
  - latest_snapshot / history read what's expected
  - growth_over returns None for missing endpoints, integers for real deltas
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from app.core import supabase_client
from app.services import instagram_metrics, stats_merge


class _StubTable:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self._rows = list(rows or [])
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.upserted: list[dict[str, Any]] = []
        self.on_conflict: str | None = None

    def _record(self, name: str, *a: Any, **k: Any) -> _StubTable:
        self.calls.append((name, a, k))
        return self

    def select(self, *a: Any, **k: Any) -> _StubTable:
        return self._record("select", *a, **k)

    def eq(self, col: str, val: Any) -> _StubTable:
        self.calls.append(("eq", (col, val), {}))
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    def gte(self, col: str, val: Any) -> _StubTable:
        self.calls.append(("gte", (col, val), {}))
        self._rows = [r for r in self._rows if str(r.get(col) or "") >= str(val)]
        return self

    def order(self, col: str, *, desc: bool = False) -> _StubTable:
        self.calls.append(("order", (col,), {"desc": desc}))
        self._rows = sorted(
            self._rows, key=lambda r: r.get(col) or "", reverse=desc
        )
        return self

    def limit(self, n: int) -> _StubTable:
        self.calls.append(("limit", (n,), {}))
        self._rows = self._rows[:n]
        return self

    def upsert(self, payload: dict[str, Any], *, on_conflict: str) -> _StubTable:
        self.upserted.append(payload)
        self.on_conflict = on_conflict
        return self

    def execute(self) -> SimpleNamespace:
        if self.upserted:
            return SimpleNamespace(data=[self.upserted[-1]])
        return SimpleNamespace(data=list(self._rows))


class _StubClient:
    def __init__(self, table: _StubTable) -> None:
        self._table = table

    def table(self, _name: str) -> _StubTable:
        return self._table


@pytest.fixture
def stub_table(monkeypatch):
    tbl = _StubTable()
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    return tbl


@pytest.fixture
def uid() -> str:
    return str(uuid4())


# ---------------------------------------------------------------------------
# snapshot_daily
# ---------------------------------------------------------------------------


def test_snapshot_daily_upserts_with_expected_shape(monkeypatch, stub_table, uid):
    monkeypatch.setattr(
        stats_merge,
        "instagram_account_snapshot",
        lambda _u: (
            {
                "followers_count": 12000,
                "follows_count": 210,
                "media_count": 87,
                "reach": 900,
                "impressions": 1400,
                "profile_views": 45,
            },
            stats_merge.IG_STATUS_OK,
        ),
    )
    stored = instagram_metrics.snapshot_daily(uid)
    assert stored is not None
    assert len(stub_table.upserted) == 1
    row = stub_table.upserted[0]
    assert row["user_id"] == uid
    assert row["followers_count"] == 12000
    assert row["reach"] == 900
    assert "captured_on" in row
    assert "captured_at" in row
    assert stub_table.on_conflict == "user_id,captured_on"


@pytest.mark.parametrize(
    "status",
    [
        stats_merge.IG_STATUS_NOT_CONFIGURED,
        stats_merge.IG_STATUS_NOT_CONNECTED,
        stats_merge.IG_STATUS_ERROR,
    ],
)
def test_snapshot_daily_skips_when_status_is_not_ok(
    monkeypatch, stub_table, uid, status
):
    monkeypatch.setattr(
        stats_merge,
        "instagram_account_snapshot",
        lambda _u: (
            dict.fromkeys(instagram_metrics.METRIC_FIELDS),
            status,
        ),
    )
    assert instagram_metrics.snapshot_daily(uid) is None
    assert stub_table.upserted == []


def test_snapshot_daily_skips_empty_snapshot(monkeypatch, stub_table, uid):
    monkeypatch.setattr(
        stats_merge,
        "instagram_account_snapshot",
        lambda _u: (
            dict.fromkeys(instagram_metrics.METRIC_FIELDS),
            stats_merge.IG_STATUS_OK,
        ),
    )
    assert instagram_metrics.snapshot_daily(uid) is None
    assert stub_table.upserted == []


def test_snapshot_daily_rejects_bad_uuid(stub_table):
    assert instagram_metrics.snapshot_daily("not-a-uuid") is None
    assert stub_table.upserted == []


# ---------------------------------------------------------------------------
# latest_snapshot / history
# ---------------------------------------------------------------------------


def test_latest_snapshot_returns_most_recent(monkeypatch, uid):
    tbl = _StubTable(
        rows=[
            {"user_id": uid, "captured_on": "2026-08-30", "followers_count": 100},
            {"user_id": uid, "captured_on": "2026-08-31", "followers_count": 105},
            {"user_id": uid, "captured_on": "2026-09-01", "followers_count": 110},
        ]
    )
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    row = instagram_metrics.latest_snapshot(uid)
    assert row is not None
    assert row["captured_on"] == "2026-09-01"
    assert row["followers_count"] == 110


def test_history_returns_newest_first_bounded_by_days(monkeypatch, uid):
    tbl = _StubTable(
        rows=[
            {"user_id": uid, "captured_on": f"2026-09-{d:02d}", "followers_count": d}
            for d in (1, 2, 3, 4)
        ]
    )
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    out = instagram_metrics.history(uid, days=2)
    assert [r["captured_on"] for r in out] == ["2026-09-04", "2026-09-03"]


# ---------------------------------------------------------------------------
# growth_over
# ---------------------------------------------------------------------------


def test_growth_over_returns_delta_across_window(monkeypatch, uid):
    tbl = _StubTable(
        rows=[
            {
                "user_id": uid,
                "captured_on": "2026-08-25",
                "followers_count": 10000,
                "follows_count": 200,
                "media_count": 80,
                "reach": 500,
                "impressions": 900,
                "profile_views": 30,
            },
            {
                "user_id": uid,
                "captured_on": "2026-09-01",
                "followers_count": 10420,
                "follows_count": 205,
                "media_count": 82,
                "reach": 700,
                "impressions": 1200,
                "profile_views": 55,
            },
        ]
    )
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    deltas = instagram_metrics.growth_over(uid, days=14)
    assert deltas["followers_count"] == 420
    assert deltas["follows_count"] == 5
    assert deltas["media_count"] == 2
    assert deltas["reach"] == 200
    assert deltas["impressions"] == 300
    assert deltas["profile_views"] == 25


def test_growth_over_returns_none_when_only_one_snapshot(monkeypatch, uid):
    tbl = _StubTable(
        rows=[
            {
                "user_id": uid,
                "captured_on": "2026-09-01",
                "followers_count": 10000,
                "reach": 700,
            }
        ]
    )
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    deltas = instagram_metrics.growth_over(uid, days=14)
    # A single data point is not growth. Every metric is None so
    # the caller renders "not enough history" instead of fabricating
    # a zero-change signal.
    assert all(v is None for v in deltas.values())


def test_growth_over_leaves_partial_metric_none(monkeypatch, uid):
    """One endpoint has reach, the other doesn't → reach delta is None."""
    tbl = _StubTable(
        rows=[
            {
                "user_id": uid,
                "captured_on": "2026-08-25",
                "followers_count": 10000,
                "reach": None,
            },
            {
                "user_id": uid,
                "captured_on": "2026-09-01",
                "followers_count": 10420,
                "reach": 700,
            },
        ]
    )
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    deltas = instagram_metrics.growth_over(uid, days=14)
    assert deltas["followers_count"] == 420
    assert deltas["reach"] is None


# ---------------------------------------------------------------------------
# verified_follower_count — Phase D convenience
# ---------------------------------------------------------------------------


def test_verified_follower_count_returns_latest_snapshot_number(
    monkeypatch, uid
):
    tbl = _StubTable(
        rows=[
            {"user_id": uid, "captured_on": "2026-09-01", "followers_count": 15321},
            {"user_id": uid, "captured_on": "2026-08-31", "followers_count": 15310},
        ]
    )
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    assert instagram_metrics.verified_follower_count(uid) == 15321


def test_verified_follower_count_none_when_no_snapshot(monkeypatch, uid):
    """No snapshot → None so the template can fall back to
    self-reported follower_range without lying."""
    tbl = _StubTable(rows=[])
    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _StubClient(tbl)
    )
    assert instagram_metrics.verified_follower_count(uid) is None


def test_verified_follower_count_none_for_bad_uuid():
    assert instagram_metrics.verified_follower_count("not-a-uuid") is None
