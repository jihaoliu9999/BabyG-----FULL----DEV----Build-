"""Tests for the babyg agent write tools."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import agent_writes


def _stub_autonomy(monkeypatch, table: dict[str, bool] | None = None):
    """Install a simple agent_can that consults `table`. Missing key -> True."""
    lookup = table or {}
    monkeypatch.setattr(
        agent_writes.agent_autonomy,
        "agent_can",
        lambda user_id, action, profile=None: lookup.get(action, True),
    )


# ---- drop_nudge ------------------------------------------------------


def test_drop_nudge_success(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes, "_count_recent_agent_nudges", lambda *a, **kw: 0
    )
    captured = {}
    monkeypatch.setattr(
        agent_writes.bot,
        "create_message",
        lambda **kwargs: captured.update(kwargs) or {"id": "msg-1"},
    )
    out = agent_writes.drop_nudge(
        "creator-1",
        body="brand acme sent 3 pitches this week.",
        category="deal_relationship",
        chips=[{"label": "draft a warm reply", "kind": "fill"}],
    )
    assert out == {"ok": True, "message_id": "msg-1"}
    assert captured["role"] == "assistant"
    assert captured["tool_calls"]["source"].startswith("agent")
    assert captured["tool_calls"]["kind"] == "nudge"
    assert captured["tool_calls"]["nudge_category"] == "deal_relationship"


def test_drop_nudge_rate_capped(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes, "_count_recent_agent_nudges", lambda *a, **kw: 4
    )
    captured: dict = {}
    monkeypatch.setattr(
        agent_writes.bot,
        "create_message",
        lambda **kwargs: captured.update({"called": True}) or {"id": "should_not_write"},
    )
    out = agent_writes.drop_nudge(
        "creator-1", body="msg", category="deal_relationship"
    )
    assert out["ok"] is False
    assert out["reason"] == "rate_capped"
    assert out["count"] == 4
    assert captured == {}  # bot.create_message never called


def test_drop_nudge_hard_cap_still_refused(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes, "_count_recent_agent_nudges", lambda *a, **kw: 25
    )
    out = agent_writes.drop_nudge(
        "creator-1", body="msg", category="whatever"
    )
    assert out == {"ok": False, "reason": "rate_capped_hard", "count": 25}


def test_drop_nudge_autonomy_denied(monkeypatch) -> None:
    _stub_autonomy(monkeypatch, {"drop_nudge": False})
    out = agent_writes.drop_nudge(
        "creator-1", body="msg", category="whatever"
    )
    assert out["ok"] is False
    assert out["reason"] == "autonomy_denied"


def test_drop_nudge_write_failure_returns_ok_false(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes, "_count_recent_agent_nudges", lambda *a, **kw: 0
    )

    def _boom(**_):
        raise RuntimeError("bot down")

    monkeypatch.setattr(agent_writes.bot, "create_message", _boom)
    out = agent_writes.drop_nudge("creator-1", body="msg", category="x")
    assert out == {"ok": False, "reason": "write_failed"}


# ---- rewrite_memory --------------------------------------------------


def test_rewrite_memory_gated(monkeypatch) -> None:
    _stub_autonomy(monkeypatch, {"rewrite_memory": False})
    out = agent_writes.rewrite_memory(
        "creator-1", "new summary", change_reason="agent init"
    )
    assert out == {
        "ok": False,
        "reason": "autonomy_denied",
        "action": "rewrite_memory",
    }


def test_rewrite_memory_success(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    captured = {}
    monkeypatch.setattr(
        agent_writes.agent_memory,
        "save",
        lambda uid, summary, updated_by, change_reason=None: captured.update(
            {
                "uid": uid,
                "summary": summary,
                "updated_by": updated_by,
                "reason": change_reason,
            }
        )
        or {"version": 7},
    )
    out = agent_writes.rewrite_memory(
        "creator-1",
        "the new summary",
        change_reason="added travel niche after 3 rewrites",
    )
    assert out == {"ok": True, "version": 7}
    assert captured["updated_by"] == "agent"
    assert captured["reason"].startswith("added travel niche")


def test_rewrite_memory_persistence_failure(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes.agent_memory,
        "save",
        lambda *a, **kw: None,
    )
    out = agent_writes.rewrite_memory(
        "creator-1", "text", change_reason="reason"
    )
    assert out == {"ok": False, "reason": "write_failed"}


# ---- update_deal_stage ----------------------------------------------


def test_update_deal_stage_gated(monkeypatch) -> None:
    _stub_autonomy(monkeypatch, {"update_deal_stage": False})
    out = agent_writes.update_deal_stage("c1", "deal1", "stale_or_ghosted")
    assert out == {
        "ok": False,
        "reason": "autonomy_denied",
        "action": "update_deal_stage",
    }


def test_update_deal_stage_success(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes.babyg_deals,
        "update_stage",
        lambda deal_id, stage, creator_id: {"id": deal_id, "stage": stage},
    )
    out = agent_writes.update_deal_stage("c1", "deal1", "stale_or_ghosted")
    assert out == {"ok": True, "deal_id": "deal1", "to_stage": "stale_or_ghosted"}


def test_update_deal_stage_refused(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes.babyg_deals,
        "update_stage",
        lambda *a, **kw: None,
    )
    out = agent_writes.update_deal_stage("c1", "deal1", "stale_or_ghosted")
    assert out == {"ok": False, "reason": "refused"}


# ---- mark_draft_stale -----------------------------------------------


def test_mark_draft_stale_gated(monkeypatch) -> None:
    _stub_autonomy(monkeypatch, {"mark_draft_stale": False})
    out = agent_writes.mark_draft_stale("c1", "draft1")
    assert out == {
        "ok": False,
        "reason": "autonomy_denied",
        "action": "mark_draft_stale",
    }


def test_mark_draft_stale_success(monkeypatch) -> None:
    _stub_autonomy(monkeypatch)
    monkeypatch.setattr(
        agent_writes.babyg_memory,
        "update_draft_status",
        lambda draft_id, status: True,
    )
    out = agent_writes.mark_draft_stale("c1", "draft1")
    assert out == {"ok": True, "draft_id": "draft1"}


# ---- idempotency ----------------------------------------------------


def test_new_idempotency_key_stable() -> None:
    k1 = agent_writes.new_idempotency_key("agent_loop", "gmail_draft", "thread-42")
    k2 = agent_writes.new_idempotency_key("agent_loop", "gmail_draft", "thread-42")
    k3 = agent_writes.new_idempotency_key("agent_loop", "gmail_draft", "thread-99")
    assert k1 == k2
    assert k1 != k3
    # Must be a uuid5 string.
    assert len(k1) == 36


# ---- rate cap window ------------------------------------------------


def test_count_recent_agent_nudges_survives_supabase_error(monkeypatch) -> None:
    class _Boom:
        def table(self, name):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(
        agent_writes.supabase_client, "get_service_client", lambda: _Boom()
    )
    # Should not raise; returns 0 so the loop can still speak.
    assert agent_writes._count_recent_agent_nudges(
        "c1", now=datetime(2026, 9, 3, tzinfo=UTC)
    ) == 0
