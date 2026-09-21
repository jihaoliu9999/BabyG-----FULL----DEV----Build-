"""Tests for the sweep runner's agent-loop cutover.

Focus: the env-flag gate on the agent loop + the filter routing.
The sweep-level logic is exercised via tests/test_bot_jobs.py; here
we only care about the runner wrapping.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

# scripts/ isn't on sys.path in the normal test collect; add it now.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import run_babyg_sweeps  # noqa: E402


def test_sentry_cron_checkins_cover_success_and_crash(monkeypatch) -> None:
    calls: list[dict] = []
    captured: list[BaseException] = []

    fake_crons = SimpleNamespace(
        capture_checkin=lambda **kwargs: calls.append(kwargs) or "check-in-1",
    )
    fake_sdk = SimpleNamespace(
        crons=fake_crons,
        capture_exception=captured.append,
        flush=lambda **kwargs: None,
    )
    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setattr(run_babyg_sweeps, "_configure_sentry", lambda: True)
    monkeypatch.setenv(
        "BABYG_SWEEPS_SENTRY_MONITOR_SLUG", "custom-sweeps-monitor"
    )

    monkeypatch.setattr(run_babyg_sweeps, "_run", lambda argv: 0)
    assert run_babyg_sweeps.main([]) == 0
    assert calls == [
        {
            "monitor_slug": "custom-sweeps-monitor",
            "status": "in_progress",
            "monitor_config": {
                "schedule": {"type": "crontab", "value": "*/15 * * * *"},
                "checkin_margin": 15,
                "max_runtime": 15,
                "timezone": "UTC",
            },
        },
        {
            "monitor_slug": "custom-sweeps-monitor",
            "check_in_id": "check-in-1",
            "status": "ok",
        },
    ]

    calls.clear()
    crash = RuntimeError("runner failed")

    def _crash(_argv):
        raise crash

    monkeypatch.setattr(run_babyg_sweeps, "_run", _crash)
    with pytest.raises(RuntimeError, match="runner failed"):
        run_babyg_sweeps.main([])
    assert calls[-1] == {
        "monitor_slug": "custom-sweeps-monitor",
        "check_in_id": "check-in-1",
        "status": "error",
    }
    assert captured == [crash]


def test_sentry_cron_checkins_are_noop_when_disabled(monkeypatch) -> None:
    monkeypatch.setattr(run_babyg_sweeps, "_configure_sentry", lambda: False)
    monkeypatch.setattr(run_babyg_sweeps, "_run", lambda argv: 0)
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk",
        SimpleNamespace(
            crons=SimpleNamespace(
                capture_checkin=lambda **kwargs: pytest.fail("unexpected check-in"),
            ),
            capture_exception=lambda exc: pytest.fail("unexpected exception"),
            flush=lambda **kwargs: pytest.fail("unexpected flush"),
        ),
    )

    assert run_babyg_sweeps.main([]) == 0


def test_agent_loop_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv("BABYG_AGENT_LOOP_ENABLED", raising=False)
    assert run_babyg_sweeps._agent_loop_enabled() is False


def test_agent_loop_enabled_by_env(monkeypatch) -> None:
    monkeypatch.setenv("BABYG_AGENT_LOOP_ENABLED", "1")
    assert run_babyg_sweeps._agent_loop_enabled() is True
    monkeypatch.setenv("BABYG_AGENT_LOOP_ENABLED", "true")
    assert run_babyg_sweeps._agent_loop_enabled() is True
    monkeypatch.setenv("BABYG_AGENT_LOOP_ENABLED", "yes")
    assert run_babyg_sweeps._agent_loop_enabled() is True
    monkeypatch.setenv("BABYG_AGENT_LOOP_ENABLED", "on")
    assert run_babyg_sweeps._agent_loop_enabled() is True


def test_agent_loop_junk_env_is_off(monkeypatch) -> None:
    monkeypatch.setenv("BABYG_AGENT_LOOP_ENABLED", "banana")
    assert run_babyg_sweeps._agent_loop_enabled() is False


def test_agent_loop_selected_no_filter() -> None:
    assert run_babyg_sweeps._agent_loop_selected("") is True


def test_agent_loop_selected_only_when_named() -> None:
    assert run_babyg_sweeps._agent_loop_selected("agent") is True
    assert run_babyg_sweeps._agent_loop_selected("Agent,ig") is True
    assert run_babyg_sweeps._agent_loop_selected("ig") is False
    assert run_babyg_sweeps._agent_loop_selected("gmail,dm") is False
