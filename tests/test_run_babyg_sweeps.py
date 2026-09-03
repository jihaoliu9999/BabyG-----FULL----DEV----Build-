"""Tests for the sweep runner's agent-loop cutover.

Focus: the env-flag gate on the agent loop + the filter routing.
The sweep-level logic is exercised via tests/test_bot_jobs.py; here
we only care about the runner wrapping.
"""

from __future__ import annotations

import sys
from pathlib import Path

# scripts/ isn't on sys.path in the normal test collect; add it now.
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import run_babyg_sweeps  # noqa: E402


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
