"""Tests for the 'while you were away' home recap.

Contract:
  - empty window -> None (template hides card)
  - proposals only, cycles only, nudges only, memory only -> each
    surfaces its own headline
  - headlines pluralized correctly
  - one table failing -> that dimension is 0, others still count
  - all four failing -> returns None (nothing to say)
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import agent_recap


class _FakeSupabase:
    def __init__(self, counts=None, raise_on=None):
        self.counts = counts or {}
        self.raise_on = raise_on or set()

    def table(self, name):
        if name in self.raise_on:
            class _Boom:
                def __getattr__(self, _):
                    raise RuntimeError(f"supabase down ({name})")
            return _Boom()
        return _FakeTable(self.counts.get(name, 0))


class _FakeTable:
    def __init__(self, count):
        self._count = count

    def select(self, *_, **__): return self
    def eq(self, *_, **__): return self
    def gte(self, *_, **__): return self
    def like(self, *_, **__): return self
    def execute(self):
        return _Result(self._count)


class _Result:
    def __init__(self, count):
        self.data = []
        self.count = count


def _install(monkeypatch, counts=None, raise_on=None) -> _FakeSupabase:
    fake = _FakeSupabase(counts=counts, raise_on=raise_on)
    monkeypatch.setattr(
        agent_recap.supabase_client, "get_service_client", lambda: fake
    )
    return fake


def test_empty_window_returns_none(monkeypatch) -> None:
    _install(monkeypatch, counts={})
    assert agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC)) is None


def test_proposals_only(monkeypatch) -> None:
    _install(monkeypatch, counts={"action_proposals": 2})
    result = agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    assert result is not None
    assert result["counts"]["proposals"] == 2
    assert any("staged 2 actions" in h for h in result["headlines"])


def test_singular_pluralization(monkeypatch) -> None:
    _install(monkeypatch, counts={"action_proposals": 1})
    result = agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    # No 's' on the single case.
    assert any("staged 1 action for your tap" in h for h in result["headlines"])


def test_all_four_dimensions_present(monkeypatch) -> None:
    _install(monkeypatch, counts={
        "action_proposals": 2,
        "agent_cycles": 4,
        "bot_messages": 3,
        "creator_agent_memory_history": 1,
    })
    result = agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    assert result["counts"] == {
        "proposals": 2,
        "cycles_active": 4,
        "nudges": 3,
        "memory_writes": 1,
    }
    assert len(result["headlines"]) == 4


def test_ordering_puts_proposals_first(monkeypatch) -> None:
    """Proposals are the most-actionable so they're the top line."""
    _install(monkeypatch, counts={
        "action_proposals": 1,
        "agent_cycles": 1,
        "bot_messages": 1,
        "creator_agent_memory_history": 1,
    })
    result = agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    assert result["headlines"][0].startswith("staged")


def test_one_table_failing_still_returns_others(monkeypatch) -> None:
    _install(
        monkeypatch,
        counts={"action_proposals": 2, "bot_messages": 1},
        raise_on={"agent_cycles"},
    )
    result = agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    assert result is not None
    assert result["counts"]["proposals"] == 2
    assert result["counts"]["cycles_active"] == 0
    assert result["counts"]["nudges"] == 1


def test_all_reads_failing_returns_none(monkeypatch) -> None:
    _install(
        monkeypatch,
        raise_on={
            "action_proposals",
            "agent_cycles",
            "bot_messages",
            "creator_agent_memory_history",
        },
    )
    assert agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC)) is None


def test_headlines_are_capped(monkeypatch) -> None:
    # Even if we somehow generated more than 6 headlines, the cap holds.
    _install(monkeypatch, counts={
        "action_proposals": 1,
        "agent_cycles": 1,
        "bot_messages": 1,
        "creator_agent_memory_history": 1,
    })
    result = agent_recap.build("c1", now=datetime(2026, 9, 3, tzinfo=UTC))
    assert len(result["headlines"]) <= agent_recap._MAX_HEADLINES


def test_window_hours_clamped_to_at_least_one(monkeypatch) -> None:
    _install(monkeypatch, counts={"action_proposals": 1})
    # A ridiculous 0-hour window should still clamp to >= 1h.
    result = agent_recap.build(
        "c1", window_hours=0, now=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert result is not None
