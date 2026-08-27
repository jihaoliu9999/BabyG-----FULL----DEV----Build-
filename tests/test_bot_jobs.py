"""Tests for app/services/bot_jobs.py.

Phase 7 of the babyg AI v2 plan (see docs/babyg-ai-reference.md).

These tests prove:

    * Sweeps are idempotent: a re-run of the same slot does not
      double-process an item.
    * Same dedupe_key produces one bot_job_runs row, never two.
    * A stale draft flips exactly once; the second sweep skips it.
    * A ghosted deal is not re-flipped once already terminal.
    * A per-item failure lands in bot_job_failures without breaking
      the rest of the sweep.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import babyg_deals, babyg_memory, bot_jobs

_CREATOR = "00000000-0000-0000-0000-000000000010"


class _FakeQuery:
    def __init__(self, store: _FakeStore, table: str) -> None:
        self._store = store
        self._table = table
        self._filters: list[tuple[str, str, Any]] = []
        self._order_col: str | None = None
        self._order_desc = True
        self._limit_n: int | None = None
        self._insert_payload: dict[str, Any] | None = None
        self._update_payload: dict[str, Any] | None = None

    def select(self, _cols: str) -> _FakeQuery:
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append(("eq", col, val))
        return self

    def in_(self, col: str, values: list[Any]) -> _FakeQuery:
        self._filters.append(("in", col, list(values)))
        return self

    def lte(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append(("lte", col, val))
        return self

    def gte(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append(("gte", col, val))
        return self

    def order(self, col: str, *, desc: bool = True) -> _FakeQuery:
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n: int) -> _FakeQuery:
        self._limit_n = n
        return self

    def insert(self, payload: dict[str, Any]) -> _FakeQuery:
        self._insert_payload = payload
        return self

    def update(self, payload: dict[str, Any]) -> _FakeQuery:
        self._update_payload = payload
        return self

    def _matches(self, row: dict[str, Any]) -> bool:
        for op, col, val in self._filters:
            if op == "eq":
                if str(row.get(col)) != str(val):
                    return False
            elif op == "in":
                if row.get(col) not in val:
                    return False
            elif op == "lte":
                v = row.get(col)
                if v is None or not (v <= val):
                    return False
            elif op == "gte":
                v = row.get(col)
                if v is None or not (v >= val):
                    return False
        return True

    def execute(self) -> Any:
        if self._insert_payload is not None:
            row = {
                "id": self._store.next_id(self._table),
                **self._insert_payload,
            }
            # Emulate the unique(job_name, dedupe_key) constraint on
            # bot_job_runs. Second write raises so mark_ran returns False.
            if self._table == "bot_job_runs":
                for existing in self._store.rows.get("bot_job_runs", []):
                    if (
                        existing.get("job_name") == row.get("job_name")
                        and existing.get("dedupe_key") == row.get("dedupe_key")
                    ):
                        raise RuntimeError(
                            "duplicate bot_job_runs (job_name, dedupe_key)"
                        )
            self._store.rows.setdefault(self._table, []).append(row)
            return type("Result", (), {"data": [row]})()

        if self._update_payload is not None:
            hit: list[dict[str, Any]] = []
            for row in self._store.rows.get(self._table, []):
                if self._matches(row):
                    row.update(self._update_payload)
                    hit.append(row)
            return type("Result", (), {"data": hit})()

        rows = [
            r for r in self._store.rows.get(self._table, []) if self._matches(r)
        ]
        if self._order_col:
            rows.sort(
                key=lambda r: r.get(self._order_col) or "",
                reverse=self._order_desc,
            )
        if self._limit_n:
            rows = rows[: self._limit_n]
        return type("Result", (), {"data": rows})()


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self._id_seq: dict[str, int] = {}

    def next_id(self, table: str) -> str:
        n = self._id_seq.get(table, 0) + 1
        self._id_seq[table] = n
        return f"00000000-0000-0000-0000-{n:012d}"

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    for module in (bot_jobs, babyg_memory, babyg_deals):
        monkeypatch.setattr(
            module.supabase_client, "get_service_client", lambda: s
        )
    return s


# ---------------------------------------------------------------------------
# Idempotence primitives
# ---------------------------------------------------------------------------


def test_already_ran_false_before_mark(store: _FakeStore) -> None:
    assert bot_jobs.already_ran("sweep_stale_drafts", "k") is False


def test_mark_ran_then_already_ran_returns_true(store: _FakeStore) -> None:
    assert bot_jobs.mark_ran("sweep_stale_drafts", "k") is True
    assert bot_jobs.already_ran("sweep_stale_drafts", "k") is True


def test_mark_ran_second_time_returns_false(store: _FakeStore) -> None:
    """Same (job_name, dedupe_key) twice is a no-op — the unique index
    on bot_job_runs enforces it and mark_ran swallows the error."""
    assert bot_jobs.mark_ran("sweep_stale_drafts", "k") is True
    assert bot_jobs.mark_ran("sweep_stale_drafts", "k") is False


def test_already_ran_scoped_by_job_name(store: _FakeStore) -> None:
    bot_jobs.mark_ran("sweep_a", "k")
    assert bot_jobs.already_ran("sweep_a", "k") is True
    assert bot_jobs.already_ran("sweep_b", "k") is False


def test_record_failure_writes_row(store: _FakeStore) -> None:
    bot_jobs.record_failure(
        "sweep_stale_drafts", RuntimeError("boom"), dedupe_key="k"
    )
    failures = store.rows.get("bot_job_failures") or []
    assert len(failures) == 1
    assert failures[0]["job_name"] == "sweep_stale_drafts"
    assert failures[0]["exception_class"] == "RuntimeError"
    assert failures[0]["exception_message"] == "boom"
    assert failures[0]["dedupe_key"] == "k"


def test_record_failure_truncates_long_message(store: _FakeStore) -> None:
    long_msg = "x" * 5000
    bot_jobs.record_failure("sweep", RuntimeError(long_msg))
    stored = store.rows["bot_job_failures"][0]["exception_message"]
    assert len(stored) == 2000


# ---------------------------------------------------------------------------
# sweep_stale_drafts
# ---------------------------------------------------------------------------


def _seed_draft(store: _FakeStore, *, updated_at: datetime, status: str = "proposed") -> str:
    draft_id = store.next_id("babyg_memory_drafts")
    store.rows.setdefault("babyg_memory_drafts", []).append(
        {
            "id": draft_id,
            "creator_id": _CREATOR,
            "channel": "email",
            "origin_tool": "gmail.create_draft",
            "subject": "s",
            "to_addr": "a@b",
            "body": "b",
            "status": status,
            "updated_at": updated_at.isoformat(),
        }
    )
    return draft_id


def test_sweep_flips_stale_draft(store: _FakeStore) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.STALE_DRAFT_DAYS + 2)
    draft_id = _seed_draft(store, updated_at=old)

    report = bot_jobs.sweep_stale_drafts(now=now)

    assert report.scanned == 1
    assert report.changed == 1
    assert report.failed == 0
    stored = store.rows["babyg_memory_drafts"][0]
    assert stored["id"] == draft_id
    assert stored["status"] == "stale"


def test_sweep_ignores_fresh_draft(store: _FakeStore) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    fresh = now - timedelta(days=1)
    _seed_draft(store, updated_at=fresh)
    report = bot_jobs.sweep_stale_drafts(now=now)
    assert report.scanned == 0
    assert store.rows["babyg_memory_drafts"][0]["status"] == "proposed"


def test_sweep_ignores_already_terminal_draft_statuses(store: _FakeStore) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.STALE_DRAFT_DAYS + 5)
    _seed_draft(store, updated_at=old, status="approved")
    _seed_draft(store, updated_at=old, status="sent")
    _seed_draft(store, updated_at=old, status="canceled")
    report = bot_jobs.sweep_stale_drafts(now=now)
    # Only proposed/edited match; none of the seeded drafts do.
    assert report.scanned == 0
    for row in store.rows["babyg_memory_drafts"]:
        assert row["status"] in {"approved", "sent", "canceled"}


def test_sweep_is_idempotent_on_rerun(store: _FakeStore) -> None:
    """Phase 7 requirement: a job that runs twice does not
    double-process. The second sweep sees the draft already flipped
    (no longer matches proposed/edited) so scanned drops to 0."""
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.STALE_DRAFT_DAYS + 2)
    _seed_draft(store, updated_at=old)

    first = bot_jobs.sweep_stale_drafts(now=now)
    second = bot_jobs.sweep_stale_drafts(now=now)

    assert first.changed == 1
    assert second.scanned == 0
    assert second.changed == 0
    # And exactly one bot_job_runs row for this draft's dedupe key.
    runs = [
        r
        for r in store.rows.get("bot_job_runs", [])
        if r["job_name"] == "sweep_stale_drafts"
    ]
    assert len(runs) == 1


def test_sweep_per_item_failure_isolated(monkeypatch, store: _FakeStore) -> None:
    """A single bad draft lands in bot_job_failures. The rest of the
    sweep still runs."""
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.STALE_DRAFT_DAYS + 3)
    a = _seed_draft(store, updated_at=old)
    _seed_draft(store, updated_at=old)

    original = bot_jobs.babyg_memory.update_draft_status

    def _bomb(draft_id, status, *, gmail_message_id=None):
        if draft_id == a:
            raise RuntimeError("db locked")
        return original(draft_id, status, gmail_message_id=gmail_message_id)

    monkeypatch.setattr(bot_jobs.babyg_memory, "update_draft_status", _bomb)

    report = bot_jobs.sweep_stale_drafts(now=now)
    assert report.scanned == 2
    assert report.changed == 1
    assert report.failed == 1
    failures = store.rows.get("bot_job_failures") or []
    assert len(failures) == 1
    assert failures[0]["dedupe_key"] == f"stale_draft:{a}"


# ---------------------------------------------------------------------------
# sweep_ghosted_deals
# ---------------------------------------------------------------------------


def _seed_deal(
    store: _FakeStore,
    *,
    stage: str,
    last_touch_at: datetime,
) -> str:
    deal_id = store.next_id("babyg_memory_deals")
    store.rows.setdefault("babyg_memory_deals", []).append(
        {
            "id": deal_id,
            "creator_id": _CREATOR,
            "brand_name": "Vans",
            "handles": [],
            "emails": [],
            "stage": stage,
            "last_touch_at": last_touch_at.isoformat(),
            "first_touch_at": last_touch_at.isoformat(),
            "notes": {},
        }
    )
    return deal_id


def test_sweep_flips_ghosted_deal(store: _FakeStore) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.GHOSTED_DEAL_DAYS + 3)
    deal_id = _seed_deal(store, stage="negotiating", last_touch_at=old)

    report = bot_jobs.sweep_ghosted_deals(now=now)

    assert report.scanned == 1
    assert report.changed == 1
    fresh = babyg_deals.get_deal(deal_id, creator_id=_CREATOR)
    assert fresh is not None
    assert fresh["stage"] == "stale_or_ghosted"


def test_sweep_ignores_recently_touched_deal(store: _FakeStore) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    fresh = now - timedelta(days=1)
    _seed_deal(store, stage="negotiating", last_touch_at=fresh)
    report = bot_jobs.sweep_ghosted_deals(now=now)
    assert report.scanned == 0


def test_sweep_ignores_terminal_deal(store: _FakeStore) -> None:
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.GHOSTED_DEAL_DAYS + 30)
    _seed_deal(store, stage="paid", last_touch_at=old)
    _seed_deal(store, stage="declined", last_touch_at=old)
    _seed_deal(store, stage="cancelled", last_touch_at=old)
    report = bot_jobs.sweep_ghosted_deals(now=now)
    # in_ filter is on _WORKING_STAGES, none of these match.
    assert report.scanned == 0


def test_sweep_ignores_delivered_and_payment_pending(store: _FakeStore) -> None:
    """These stages mean 'waiting on the money', not 'brand went
    quiet'. Do not roll them to ghosted."""
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.GHOSTED_DEAL_DAYS + 30)
    _seed_deal(store, stage="delivered", last_touch_at=old)
    _seed_deal(store, stage="payment_pending", last_touch_at=old)
    report = bot_jobs.sweep_ghosted_deals(now=now)
    assert report.scanned == 0


def test_ghosted_sweep_idempotent_same_day(store: _FakeStore) -> None:
    """Twice in the same day is a no-op on the second run for the same
    deal."""
    now = datetime(2027, 1, 1, tzinfo=UTC)
    old = now - timedelta(days=bot_jobs.GHOSTED_DEAL_DAYS + 3)
    _seed_deal(store, stage="negotiating", last_touch_at=old)

    first = bot_jobs.sweep_ghosted_deals(now=now)
    # After the first sweep the deal is stale_or_ghosted, so the second
    # sweep's in_() filter no longer matches it — scanned=0.
    second = bot_jobs.sweep_ghosted_deals(now=now)

    assert first.changed == 1
    assert second.scanned == 0


# ---------------------------------------------------------------------------
# run_all
# ---------------------------------------------------------------------------


def test_run_all_returns_report_per_sweep(store: _FakeStore) -> None:
    reports = bot_jobs.run_all()
    names = {r.job_name for r in reports}
    assert names == {"sweep_stale_drafts", "sweep_ghosted_deals"}


def test_run_all_records_failure_when_a_sweep_crashes(monkeypatch, store: _FakeStore) -> None:
    def _boom(*, now=None):
        raise RuntimeError("sweep crashed")

    monkeypatch.setattr(bot_jobs, "sweep_stale_drafts", _boom)
    reports = bot_jobs.run_all()
    stale = [r for r in reports if r.job_name == "_boom"]
    assert stale, "the crashed sweep still returns a report"
    assert stale[0].failed == 1
    failures = store.rows.get("bot_job_failures") or []
    assert any(f["exception_class"] == "RuntimeError" for f in failures)
