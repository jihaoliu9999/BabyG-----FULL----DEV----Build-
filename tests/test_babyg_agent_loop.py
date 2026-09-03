"""Tests for the babyg background agent loop."""

from __future__ import annotations

from datetime import UTC, datetime

from app.integrations.anthropic_client import (
    ClaudeCallError,
    ClaudeNotConfiguredError,
    ClaudeResponse,
)
from app.services import babyg_agent_loop


def _stub_common(monkeypatch, *, profile=None, memory=None):
    monkeypatch.setattr(
        babyg_agent_loop.profiles,
        "get_creator_profile",
        lambda uid: profile or {"id": uid},
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_memory, "load", lambda uid: memory
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_autonomy,
        "load_settings",
        lambda uid, profile=None: {
            "internal_actions": True,
            "gmail_auto_send": False,
            "calendar_holds": False,
        },
    )


def _capture_cycle(monkeypatch) -> list[dict]:
    captured: list[dict] = []
    monkeypatch.setattr(
        babyg_agent_loop.agent_cycles,
        "record_cycle",
        lambda user_id, **kwargs: captured.append({"user_id": user_id, **kwargs})
        or {"id": "cycle-1"},
    )
    return captured


def test_over_daily_cap_skips_before_llm(monkeypatch) -> None:
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: True
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "daily_cap_usd", lambda: 0.10
    )
    calls = {"observe": 0, "claude": 0}
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: calls.update(observe=calls["observe"] + 1) or {},
    )

    def _boom_claude(**_):
        calls["claude"] += 1
        raise AssertionError("should not have called claude")

    monkeypatch.setattr(babyg_agent_loop, "complete_chat", _boom_claude)
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle("creator-1")
    assert result["status"] == "skipped_over_cap"
    assert calls == {"observe": 0, "claude": 0}
    assert captured[0]["status"] == "skipped_over_cap"


def test_no_delta_skips_llm(monkeypatch) -> None:
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: False
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: {
            "stale_drafts": [],
            "ghosted_deals": [],
            "upcoming_bookings": [],
            "unread_dms": {"count": 0},
            "pending_action_proposals": {"count": 0},
            "as_of": "2026-09-03T00:00:00Z",
        },
    )

    def _boom_claude(**_):
        raise AssertionError("should not have called claude")

    monkeypatch.setattr(babyg_agent_loop, "complete_chat", _boom_claude)
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle("creator-1")
    assert result["status"] == "skipped_no_delta"
    assert captured[0]["delta"] == {
        "stale_drafts": 0,
        "ghosted_deals": 0,
        "upcoming_bookings": 0,
        "unread_dms": 0,
        "pending_action_proposals": 0,
    }


def test_ok_cycle_dispatches_tool_calls(monkeypatch) -> None:
    _stub_common(monkeypatch, memory={"summary": "creator base", "version": 3})
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: False
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: {
            "stale_drafts": [{"id": "d1", "brand_name": "acme", "status": "proposed"}],
            "ghosted_deals": [],
            "upcoming_bookings": [],
            "unread_dms": {"count": 0},
            "pending_action_proposals": {"count": 0},
            "as_of": "2026-09-03T00:00:00Z",
        },
    )

    fake_response = ClaudeResponse(
        text="marking one stale draft.",
        content=[
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "mark_draft_stale",
                "input": {"draft_id": "d1"},
            },
            {
                "type": "tool_use",
                "id": "toolu_2",
                "name": "drop_nudge",
                "input": {"body": "cleaned up one stale draft.", "category": "housekeeping"},
            },
        ],
        stop_reason="end_turn",
        input_tokens=800,
        output_tokens=120,
    )
    monkeypatch.setattr(
        babyg_agent_loop, "complete_chat", lambda **kwargs: fake_response
    )

    dispatched: list[dict] = []

    def _fake_mark_draft(user_id, args, profile):
        dispatched.append(("mark_draft_stale", args))
        return {"ok": True, "draft_id": args["draft_id"]}

    def _fake_drop_nudge(user_id, args, profile):
        dispatched.append(("drop_nudge", args))
        return {"ok": True, "message_id": "msg1"}

    monkeypatch.setitem(
        babyg_agent_loop._TOOL_DISPATCH, "mark_draft_stale", _fake_mark_draft
    )
    monkeypatch.setitem(
        babyg_agent_loop._TOOL_DISPATCH, "drop_nudge", _fake_drop_nudge
    )

    monkeypatch.setattr(
        babyg_agent_loop.agent_cost,
        "estimate_cost_usd",
        lambda p, c, m: 0.005,
    )
    recorded_cost: dict = {}
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost,
        "record_cycle",
        lambda user_id, **kw: recorded_cost.update(user_id=user_id, **kw)
        or {"day": "d", "cost_usd": 0.005},
    )
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle(
        "creator-1", now=datetime(2026, 9, 3, tzinfo=UTC)
    )
    assert result["status"] == "ok"
    assert [name for name, _ in dispatched] == ["mark_draft_stale", "drop_nudge"]
    assert recorded_cost["prompt_tokens"] == 800
    assert recorded_cost["completion_tokens"] == 120
    tool_names = [t["name"] for t in captured[0]["tools_called"]]
    assert tool_names == ["mark_draft_stale", "drop_nudge"]


def test_claude_not_configured_records_skip(monkeypatch) -> None:
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: False
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: {
            "stale_drafts": [{"id": "d1"}],
            "ghosted_deals": [],
            "upcoming_bookings": [],
            "unread_dms": {"count": 0},
            "pending_action_proposals": {"count": 0},
            "as_of": "2026-09-03T00:00:00Z",
        },
    )

    def _raise(**_):
        raise ClaudeNotConfiguredError("key missing")

    monkeypatch.setattr(babyg_agent_loop, "complete_chat", _raise)
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle("creator-1")
    assert result["status"] == "skipped_no_delta"
    assert captured[0]["skip_reason"] == "anthropic_api_key not configured"


def test_claude_call_error_records_failed(monkeypatch) -> None:
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: False
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: {
            "stale_drafts": [{"id": "d1"}],
            "ghosted_deals": [],
            "upcoming_bookings": [],
            "unread_dms": {"count": 0},
            "pending_action_proposals": {"count": 0},
            "as_of": "2026-09-03T00:00:00Z",
        },
    )

    def _raise(**_):
        raise ClaudeCallError("upstream 500")

    monkeypatch.setattr(babyg_agent_loop, "complete_chat", _raise)
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle("creator-1")
    assert result["status"] == "failed"
    assert captured[0]["error_class"] == "ClaudeCallError"


def test_tool_exception_becomes_failed_tool_result(monkeypatch) -> None:
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: False
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: {
            "stale_drafts": [{"id": "d1"}],
            "ghosted_deals": [],
            "upcoming_bookings": [],
            "unread_dms": {"count": 0},
            "pending_action_proposals": {"count": 0},
            "as_of": "2026-09-03T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        babyg_agent_loop,
        "complete_chat",
        lambda **_: ClaudeResponse(
            text="",
            content=[
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "mark_draft_stale",
                    "input": {"draft_id": "d1"},
                }
            ],
            input_tokens=100,
            output_tokens=10,
        ),
    )

    def _boom(user_id, args, profile):
        raise RuntimeError("db down")

    monkeypatch.setitem(babyg_agent_loop._TOOL_DISPATCH, "mark_draft_stale", _boom)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "estimate_cost_usd", lambda *a: 0.001
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "record_cycle", lambda *a, **kw: {}
    )
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle("creator-1")
    # Cycle still 'ok' because we made a claude call successfully;
    # the individual tool failed but that lands in tools_called.
    assert result["status"] == "ok"
    outcome = captured[0]["tools_called"][0]["outcome"]
    assert outcome["ok"] is False
    assert outcome["reason"] == "exception"
    assert outcome["class"] == "RuntimeError"


def test_unknown_tool_name_survives(monkeypatch) -> None:
    _stub_common(monkeypatch)
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "over_daily_cap", lambda uid: False
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_tools,
        "observe",
        lambda uid, now=None: {
            "stale_drafts": [{"id": "d1"}],
            "ghosted_deals": [],
            "upcoming_bookings": [],
            "unread_dms": {"count": 0},
            "pending_action_proposals": {"count": 0},
            "as_of": "2026-09-03T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        babyg_agent_loop,
        "complete_chat",
        lambda **_: ClaudeResponse(
            text="",
            content=[
                {"type": "tool_use", "id": "t", "name": "delete_universe", "input": {}}
            ],
            input_tokens=100,
            output_tokens=10,
        ),
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "estimate_cost_usd", lambda *a: 0.001
    )
    monkeypatch.setattr(
        babyg_agent_loop.agent_cost, "record_cycle", lambda *a, **kw: {}
    )
    captured = _capture_cycle(monkeypatch)

    result = babyg_agent_loop.run_cycle("creator-1")
    assert result["status"] == "ok"
    assert captured[0]["tools_called"][0]["outcome"] == "unknown_tool"


def test_run_for_all_creators_iterates(monkeypatch) -> None:
    monkeypatch.setattr(
        babyg_agent_loop, "_active_creator_ids", lambda limit: ["c1", "c2", "c3"]
    )
    fired: list[str] = []

    def _fake_cycle(user_id, now=None):
        fired.append(user_id)
        return {"user_id": user_id, "status": "skipped_no_delta"}

    monkeypatch.setattr(babyg_agent_loop, "run_cycle", _fake_cycle)
    out = babyg_agent_loop.run_for_all_creators()
    assert fired == ["c1", "c2", "c3"]
    assert [r["status"] for r in out] == ["skipped_no_delta"] * 3


def test_run_for_all_creators_survives_per_creator_crash(monkeypatch) -> None:
    monkeypatch.setattr(
        babyg_agent_loop, "_active_creator_ids", lambda limit: ["c1", "c2"]
    )

    def _fake_cycle(user_id, now=None):
        if user_id == "c1":
            raise RuntimeError("boom")
        return {"user_id": user_id, "status": "ok"}

    monkeypatch.setattr(babyg_agent_loop, "run_cycle", _fake_cycle)
    out = babyg_agent_loop.run_for_all_creators()
    assert out[0]["status"] == "failed"
    assert out[1]["status"] == "ok"


def test_serialize_observation_skips_empty_dimensions() -> None:
    obs = {
        "as_of": "2026-09-03T00:00:00Z",
        "stale_drafts": [{"id": "d1", "brand_name": "acme", "status": "proposed"}],
        "ghosted_deals": [],
        "upcoming_bookings": [],
        "unread_dms": {"count": 0},
        "pending_action_proposals": {"count": 0},
    }
    out = babyg_agent_loop._serialize_observation(obs)
    assert "draft(s) sitting" in out
    assert "unread DM" not in out
    assert "upcoming booking" not in out


def test_serialize_observation_no_delta_marker() -> None:
    empty = {"as_of": "..."}
    assert (
        babyg_agent_loop._serialize_observation(empty) == "(no delta this cycle)"
    )


def test_agent_model_env_override(monkeypatch) -> None:
    monkeypatch.setenv("BABYG_AGENT_MODEL", "claude-sonnet-4-20250514")
    assert babyg_agent_loop.agent_model() == "claude-sonnet-4-20250514"
    monkeypatch.delenv("BABYG_AGENT_MODEL", raising=False)
    assert babyg_agent_loop.agent_model() == babyg_agent_loop.DEFAULT_AGENT_MODEL
