"""Tests for the agent_cycles trace writer."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import agent_cycles


class _FakeSupabase:
    def __init__(self, response_rows=None, raise_on=None):
        self.inserted: list[dict] = []
        self.select_calls: list[dict] = []
        self._response_rows = response_rows if response_rows is not None else []
        self._raise_on = raise_on

    def table(self, name):
        assert name == "agent_cycles"
        return _FakeTable(self)


class _FakeTable:
    def __init__(self, store):
        self.store = store
        self._filter = {}
        self._order = None
        self._limit = None
        self._insert_row = None

    def insert(self, row):
        self._insert_row = row
        return self

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def order(self, col, desc=False):
        self._order = (col, desc)
        return self

    def limit(self, n):
        self._limit = n
        return self

    def execute(self):
        if self.store._raise_on == "insert" and self._insert_row is not None:
            raise RuntimeError("supabase down (insert)")
        if self.store._raise_on == "select" and self._insert_row is None:
            raise RuntimeError("supabase down (select)")
        if self._insert_row is not None:
            self.store.inserted.append(self._insert_row)
            return _Result([self._insert_row])
        return _Result(self.store._response_rows)


class _Result:
    def __init__(self, data):
        self.data = data


def _install_fake(monkeypatch, **kwargs) -> _FakeSupabase:
    fake = _FakeSupabase(**kwargs)
    monkeypatch.setattr(
        agent_cycles.supabase_client, "get_service_client", lambda: fake
    )
    return fake


def test_record_cycle_writes_ok_row(monkeypatch) -> None:
    fake = _install_fake(monkeypatch)
    started = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    ended = datetime(2026, 9, 3, 12, 0, 5, tzinfo=UTC)
    result = agent_cycles.record_cycle(
        "creator-1",
        status="ok",
        cycle_started_at=started,
        cycle_ended_at=ended,
        delta={"new_gmail": 3},
        tools_called=[{"name": "draft_gmail_reply", "outcome": "ok"}],
        final_response="drafted 2 replies for you.",
        system_prompt_hash="abc123",
        model="claude-haiku-4-5-20251001",
        prompt_tokens=1_200,
        completion_tokens=300,
        cost_usd=0.0033,
    )
    assert result is not None
    row = fake.inserted[0]
    assert row["status"] == "ok"
    assert row["user_id"] == "creator-1"
    assert row["delta"] == {"new_gmail": 3}
    assert row["tools_called"][0]["name"] == "draft_gmail_reply"
    assert row["prompt_tokens"] == 1_200
    assert row["completion_tokens"] == 300
    assert row["cost_usd"] == 0.0033
    assert row["cycle_ended_at"].startswith("2026-09-03T12:00:05")


def test_record_cycle_rejects_bad_status(monkeypatch, caplog) -> None:
    _install_fake(monkeypatch)
    import logging

    with caplog.at_level(logging.WARNING):
        result = agent_cycles.record_cycle(
            "creator-1",
            status="mystery",
            cycle_started_at=datetime.now(UTC),
        )
    assert result is None
    assert any("bad_status" in r.message for r in caplog.records)


def test_record_cycle_truncates_long_fields(monkeypatch) -> None:
    fake = _install_fake(monkeypatch)
    long_reason = "x" * 800
    long_error = "y" * 3000
    agent_cycles.record_cycle(
        "creator-1",
        status="failed",
        cycle_started_at=datetime.now(UTC),
        skip_reason=long_reason,
        error_class="Z" * 200,
        error_message=long_error,
    )
    row = fake.inserted[0]
    assert len(row["skip_reason"]) == 500
    assert len(row["error_class"]) == 120
    assert len(row["error_message"]) == 2000


def test_record_cycle_swallows_supabase_error(monkeypatch) -> None:
    _install_fake(monkeypatch, raise_on="insert")
    result = agent_cycles.record_cycle(
        "creator-1",
        status="ok",
        cycle_started_at=datetime.now(UTC),
    )
    # No exception up the stack; None returned so the loop can move on.
    assert result is None


def test_record_cycle_clamps_negative_token_counts(monkeypatch) -> None:
    fake = _install_fake(monkeypatch)
    agent_cycles.record_cycle(
        "creator-1",
        status="ok",
        cycle_started_at=datetime.now(UTC),
        prompt_tokens=-5,
        completion_tokens=-10,
        cost_usd=-0.001,
    )
    row = fake.inserted[0]
    assert row["prompt_tokens"] == 0
    assert row["completion_tokens"] == 0
    assert row["cost_usd"] == 0.0


def test_list_recent_returns_stored_rows(monkeypatch) -> None:
    fake_rows = [
        {"id": "c1", "status": "ok", "cost_usd": 0.001},
        {"id": "c2", "status": "skipped_no_delta", "cost_usd": 0.0},
    ]
    _install_fake(monkeypatch, response_rows=fake_rows)
    result = agent_cycles.list_recent("creator-1", limit=5)
    assert [r["id"] for r in result] == ["c1", "c2"]


def test_list_recent_clamps_limit(monkeypatch) -> None:
    _install_fake(monkeypatch, response_rows=[])
    # No assertion possible on limit passed since our fake doesn't
    # capture it; just verify it doesn't raise on extremes.
    assert agent_cycles.list_recent("creator-1", limit=99_999) == []
    assert agent_cycles.list_recent("creator-1", limit=0) == []


def test_list_recent_swallows_error(monkeypatch) -> None:
    _install_fake(monkeypatch, raise_on="select")
    assert agent_cycles.list_recent("creator-1") == []


def test_latest_returns_first_or_none(monkeypatch) -> None:
    _install_fake(monkeypatch, response_rows=[{"id": "c1", "status": "ok"}])
    assert agent_cycles.latest("creator-1")["id"] == "c1"

    _install_fake(monkeypatch, response_rows=[])
    assert agent_cycles.latest("creator-1") is None


def test_prompt_hash_stable_and_short() -> None:
    h1 = agent_cycles.prompt_hash("you are babyg the manager.")
    h2 = agent_cycles.prompt_hash("you are babyg the manager.")
    h3 = agent_cycles.prompt_hash("you are babyg the operator.")
    assert h1 == h2
    assert h1 != h3
    assert len(h1) == 12
