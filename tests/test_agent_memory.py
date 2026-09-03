"""Tests for the durable creator memory service."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import agent_memory


class _FakeSupabase:
    def __init__(self):
        self.memory_rows: dict[str, dict] = {}
        self.history_rows: list[dict] = []
        self.raise_on_memory_write = False
        self.raise_on_history_write = False
        self.raise_on_memory_read = False

    def table(self, name):
        if name == "creator_agent_memory":
            return _FakeMemoryTable(self)
        if name == "creator_agent_memory_history":
            return _FakeHistoryTable(self)
        raise AssertionError(f"unexpected table {name}")


class _FakeMemoryTable:
    def __init__(self, store):
        self.store = store
        self._filter: dict = {}
        self._upsert: dict | None = None

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def limit(self, n):
        return self

    def upsert(self, body, on_conflict=None):
        self._upsert = body
        return self

    def execute(self):
        if self._upsert is not None:
            if self.store.raise_on_memory_write:
                raise RuntimeError("supabase down (memory upsert)")
            self.store.memory_rows[self._upsert["user_id"]] = self._upsert
            return _Result([self._upsert])
        if self.store.raise_on_memory_read:
            raise RuntimeError("supabase down (memory read)")
        row = self.store.memory_rows.get(self._filter.get("user_id"))
        return _Result([row] if row else [])


class _FakeHistoryTable:
    def __init__(self, store):
        self.store = store
        self._insert: dict | None = None
        self._filter: dict = {}

    def insert(self, row):
        self._insert = row
        return self

    def select(self, cols):
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def order(self, col, desc=False):
        return self

    def limit(self, n):
        return self

    def execute(self):
        if self._insert is not None:
            if self.store.raise_on_history_write:
                raise RuntimeError("supabase down (history insert)")
            self.store.history_rows.append(self._insert)
            return _Result([self._insert])
        return _Result(
            list(
                reversed(
                    [
                        r
                        for r in self.store.history_rows
                        if r.get("user_id") == self._filter.get("user_id")
                    ]
                )
            )
        )


class _Result:
    def __init__(self, data):
        self.data = data


def _install(monkeypatch) -> _FakeSupabase:
    fake = _FakeSupabase()
    monkeypatch.setattr(
        agent_memory.supabase_client, "get_service_client", lambda: fake
    )
    return fake


def test_load_returns_none_for_fresh_creator(monkeypatch) -> None:
    _install(monkeypatch)
    assert agent_memory.load("creator-1") is None


def test_save_creates_first_version_and_history(monkeypatch) -> None:
    fake = _install(monkeypatch)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    row = agent_memory.save(
        "creator-1",
        "travel + fashion creator based in nyc.",
        updated_by="agent",
        change_reason="initial write from onboarding data",
        now=now,
    )
    assert row is not None
    assert row["version"] == 1
    assert row["updated_by"] == "agent"
    assert row["summary"].startswith("travel + fashion")
    assert fake.memory_rows["creator-1"]["version"] == 1
    assert fake.history_rows[0]["version"] == 1
    assert fake.history_rows[0]["change_reason"] == (
        "initial write from onboarding data"
    )


def test_save_bumps_version_on_each_call(monkeypatch) -> None:
    fake = _install(monkeypatch)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)
    agent_memory.save("creator-1", "v1", updated_by="agent", now=now)
    agent_memory.save("creator-1", "v2", updated_by="user", now=now)
    agent_memory.save("creator-1", "v3", updated_by="agent", now=now)
    assert fake.memory_rows["creator-1"]["version"] == 3
    assert fake.memory_rows["creator-1"]["summary"] == "v3"
    assert fake.memory_rows["creator-1"]["updated_by"] == "agent"
    assert [h["version"] for h in fake.history_rows] == [1, 2, 3]


def test_save_clamps_summary_to_max_chars(monkeypatch) -> None:
    fake = _install(monkeypatch)
    huge = "z" * (agent_memory.SUMMARY_MAX_CHARS + 500)
    agent_memory.save("creator-1", huge, updated_by="user")
    assert len(fake.memory_rows["creator-1"]["summary"]) == agent_memory.SUMMARY_MAX_CHARS


def test_save_rejects_bad_updated_by(monkeypatch, caplog) -> None:
    _install(monkeypatch)
    import logging

    with caplog.at_level(logging.WARNING):
        result = agent_memory.save("creator-1", "text", updated_by="operator")
    assert result is None
    assert any("bad_updated_by" in r.message for r in caplog.records)


def test_save_returns_row_even_when_history_write_fails(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.raise_on_history_write = True
    result = agent_memory.save(
        "creator-1", "kept the current row", updated_by="agent"
    )
    # Current-state upsert succeeded, history did not — that's the
    # documented behavior. Loop must not lose the summary because
    # the audit trail hiccuped.
    assert result is not None
    assert fake.memory_rows["creator-1"]["summary"] == "kept the current row"
    assert fake.history_rows == []


def test_save_returns_none_when_memory_write_fails(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.raise_on_memory_write = True
    result = agent_memory.save("creator-1", "text", updated_by="agent")
    assert result is None


def test_history_orders_newest_first(monkeypatch) -> None:
    _install(monkeypatch)
    agent_memory.save("creator-1", "v1", updated_by="agent")
    agent_memory.save("creator-1", "v2", updated_by="user")
    history = agent_memory.history("creator-1", limit=5)
    # newest-first: v2 before v1
    assert [h["version"] for h in history] == [2, 1]


def test_history_swallows_error(monkeypatch) -> None:
    class _Boom:
        def table(self, name):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(agent_memory.supabase_client, "get_service_client", lambda: _Boom())
    assert agent_memory.history("creator-1") == []


def test_load_swallows_error(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.raise_on_memory_read = True
    assert agent_memory.load("creator-1") is None
