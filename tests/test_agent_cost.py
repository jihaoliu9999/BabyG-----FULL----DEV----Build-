"""Tests for the per-creator daily cost cap.

Exercises the shape of agent_cost without touching supabase:

- estimate_cost_usd for known + unknown models
- daily_cap_usd default + env override + garbage env
- today_spend returns zeros for a fresh creator
- over_daily_cap thresholds correctly
- record_cycle increments existing counters and upserts
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from app.services import agent_cost


class _FakeSupabase:
    """In-memory stand-in for the agent_daily_spend table."""

    def __init__(self):
        self.rows: dict[tuple[str, str], dict] = {}
        self.upsert_calls: list[dict] = []

    def table(self, name):
        assert name == "agent_daily_spend"
        return _FakeTable(self)


class _FakeTable:
    def __init__(self, store):
        self.store = store
        self._filter: dict = {}
        self._select_cols: str | None = None
        self._upsert_payload: dict | None = None

    def select(self, cols):
        self._select_cols = cols
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def limit(self, n):
        return self

    def upsert(self, payload, on_conflict=None):
        self._upsert_payload = payload
        return self

    def execute(self):
        if self._upsert_payload is not None:
            key = (self._upsert_payload["user_id"], self._upsert_payload["day"])
            self.store.rows[key] = self._upsert_payload
            self.store.upsert_calls.append(self._upsert_payload)
            return _Result([self._upsert_payload])
        key = (self._filter.get("user_id"), self._filter.get("day"))
        row = self.store.rows.get(key)
        return _Result([row] if row else [])


class _Result:
    def __init__(self, data):
        self.data = data


def _install_fake(monkeypatch) -> _FakeSupabase:
    fake = _FakeSupabase()
    monkeypatch.setattr(
        agent_cost.supabase_client, "get_service_client", lambda: fake
    )
    return fake


def test_estimate_cost_known_model() -> None:
    # Haiku prices: $1 / 1M input, $5 / 1M output.
    cost = agent_cost.estimate_cost_usd(
        prompt_tokens=10_000,
        completion_tokens=2_000,
        model="claude-haiku-4-5-20251001",
    )
    assert abs(cost - (0.01 + 0.01)) < 1e-6


def test_estimate_cost_unknown_model_falls_back_conservatively(caplog) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        cost = agent_cost.estimate_cost_usd(
            prompt_tokens=10_000,
            completion_tokens=0,
            model="claude-unknown-2099",
        )
    # Falls back to Haiku ($1 / 1M input) -> $0.01 for 10k prompt tokens.
    assert abs(cost - 0.01) < 1e-6
    assert any("unknown_model" in r.message for r in caplog.records)


def test_daily_cap_default(monkeypatch) -> None:
    monkeypatch.delenv("BABYG_AGENT_DAILY_CAP_USD", raising=False)
    assert agent_cost.daily_cap_usd() == agent_cost.DEFAULT_DAILY_CAP_USD


def test_daily_cap_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BABYG_AGENT_DAILY_CAP_USD", "0.42")
    assert agent_cost.daily_cap_usd() == 0.42


def test_daily_cap_garbage_env_falls_back(monkeypatch, caplog) -> None:
    monkeypatch.setenv("BABYG_AGENT_DAILY_CAP_USD", "not_a_number")
    import logging

    with caplog.at_level(logging.WARNING):
        cap = agent_cost.daily_cap_usd()
    assert cap == agent_cost.DEFAULT_DAILY_CAP_USD
    assert any("bad_cap_env" in r.message for r in caplog.records)


def test_today_spend_empty_for_fresh_creator(monkeypatch) -> None:
    _install_fake(monkeypatch)
    row = agent_cost.today_spend("creator-1", today=date(2026, 9, 3))
    assert row["cost_usd"] == 0.0
    assert row["cycles_run"] == 0
    assert row["prompt_tokens"] == 0
    assert row["day"] == "2026-09-03"


def test_over_daily_cap_below_and_at_threshold(monkeypatch) -> None:
    monkeypatch.delenv("BABYG_AGENT_DAILY_CAP_USD", raising=False)
    fake = _install_fake(monkeypatch)
    today = date(2026, 9, 3)
    fake.rows[("creator-1", today.isoformat())] = {
        "user_id": "creator-1",
        "day": today.isoformat(),
        "prompt_tokens": 100,
        "completion_tokens": 50,
        "cost_usd": 0.099,
        "cycles_run": 3,
    }
    assert agent_cost.over_daily_cap("creator-1", today=today) is False

    fake.rows[("creator-1", today.isoformat())]["cost_usd"] = 0.10
    assert agent_cost.over_daily_cap("creator-1", today=today) is True


def test_over_daily_cap_zero_cap_disables_agent(monkeypatch) -> None:
    monkeypatch.setenv("BABYG_AGENT_DAILY_CAP_USD", "0")
    _install_fake(monkeypatch)
    assert agent_cost.over_daily_cap("creator-1") is True


def test_record_cycle_upserts_and_increments(monkeypatch) -> None:
    fake = _install_fake(monkeypatch)
    today = date(2026, 9, 3)
    now = datetime(2026, 9, 3, 12, 0, tzinfo=UTC)

    agent_cost.record_cycle(
        "creator-1",
        prompt_tokens=1_000,
        completion_tokens=500,
        cost_usd=0.005,
        today=today,
        now=now,
    )
    agent_cost.record_cycle(
        "creator-1",
        prompt_tokens=2_000,
        completion_tokens=800,
        cost_usd=0.009,
        today=today,
        now=now,
    )
    stored = fake.rows[("creator-1", "2026-09-03")]
    assert stored["prompt_tokens"] == 3_000
    assert stored["completion_tokens"] == 1_300
    assert abs(stored["cost_usd"] - 0.014) < 1e-6
    assert stored["cycles_run"] == 2


def test_record_cycle_swallows_write_failure(monkeypatch) -> None:
    class _Boom:
        def table(self, _):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(agent_cost.supabase_client, "get_service_client", lambda: _Boom())
    result = agent_cost.record_cycle(
        "creator-1", prompt_tokens=1, completion_tokens=1, cost_usd=0.001
    )
    assert result is None
