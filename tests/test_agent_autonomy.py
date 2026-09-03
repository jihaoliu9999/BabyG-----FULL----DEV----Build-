"""Tests for the babyg agent autonomy gate."""

from __future__ import annotations

import pytest

from app.services import agent_autonomy


@pytest.fixture
def stub_profile(monkeypatch):
    """Give every test a controllable profile without touching supabase."""

    def _install(**overrides):
        base = {
            "id": "creator-1",
            "babyg_agent_internal_actions": True,
            "babyg_agent_gmail_auto_send": False,
            "babyg_agent_calendar_holds": False,
        }
        base.update(overrides)
        monkeypatch.setattr(
            agent_autonomy.profiles,
            "get_creator_profile",
            lambda uid: base,
        )
        return base

    return _install


def test_nudges_and_drafts_always_allowed(stub_profile) -> None:
    """The baseline: dropping a nudge or staging a proposal is
    ALWAYS allowed, even when every autonomy switch is off."""
    stub_profile(
        babyg_agent_internal_actions=False,
        babyg_agent_gmail_auto_send=False,
        babyg_agent_calendar_holds=False,
    )
    assert agent_autonomy.agent_can("creator-1", "drop_nudge") is True
    assert agent_autonomy.agent_can("creator-1", "stage_action_proposal") is True
    assert agent_autonomy.agent_can("creator-1", "snapshot_metrics") is True
    assert agent_autonomy.agent_can("creator-1", "generate_dm_brief") is True


def test_internal_actions_gate(stub_profile) -> None:
    stub_profile(babyg_agent_internal_actions=True)
    assert agent_autonomy.agent_can("creator-1", "update_deal_stage") is True
    assert agent_autonomy.agent_can("creator-1", "rewrite_memory") is True

    stub_profile(babyg_agent_internal_actions=False)
    assert agent_autonomy.agent_can("creator-1", "update_deal_stage") is False
    assert agent_autonomy.agent_can("creator-1", "mark_draft_stale") is False


def test_gmail_auto_send_gate(stub_profile) -> None:
    stub_profile(babyg_agent_gmail_auto_send=False)
    assert agent_autonomy.agent_can("creator-1", "gmail_auto_reply") is False

    stub_profile(babyg_agent_gmail_auto_send=True)
    assert agent_autonomy.agent_can("creator-1", "gmail_auto_reply") is True


def test_calendar_holds_gate(stub_profile) -> None:
    stub_profile(babyg_agent_calendar_holds=False)
    assert agent_autonomy.agent_can("creator-1", "calendar_create_hold") is False

    stub_profile(babyg_agent_calendar_holds=True)
    assert agent_autonomy.agent_can("creator-1", "calendar_create_hold") is True


def test_unknown_action_refused_with_warning(stub_profile, caplog) -> None:
    stub_profile()
    import logging

    with caplog.at_level(logging.WARNING):
        result = agent_autonomy.agent_can("creator-1", "not_a_real_action")
    assert result is False
    assert any("unknown_action" in r.message for r in caplog.records)


def test_accepts_prefetched_profile(monkeypatch) -> None:
    """When a caller already loaded the profile once per cycle, they
    can pass it in to skip a supabase round-trip."""
    calls = {"n": 0}

    def _get(_):
        calls["n"] += 1
        return {"babyg_agent_internal_actions": True}

    monkeypatch.setattr(agent_autonomy.profiles, "get_creator_profile", _get)
    profile = {"babyg_agent_gmail_auto_send": True}
    assert (
        agent_autonomy.agent_can(
            "creator-1", "gmail_auto_reply", profile=profile
        )
        is True
    )
    assert calls["n"] == 0


def test_missing_columns_fall_back_to_documented_defaults(monkeypatch) -> None:
    """A creator whose row predates migration 0034 has no autonomy
    columns. internal_actions should default to True (preserves
    today's sweep behavior); external switches default to False."""
    monkeypatch.setattr(
        agent_autonomy.profiles,
        "get_creator_profile",
        lambda uid: {"id": "old-creator"},
    )
    assert agent_autonomy.agent_can("old-creator", "update_deal_stage") is True
    assert agent_autonomy.agent_can("old-creator", "gmail_auto_reply") is False
    assert agent_autonomy.agent_can("old-creator", "calendar_create_hold") is False


def test_load_settings_returns_three_booleans(stub_profile) -> None:
    stub_profile(
        babyg_agent_internal_actions=True,
        babyg_agent_gmail_auto_send=True,
        babyg_agent_calendar_holds=False,
    )
    settings = agent_autonomy.load_settings("creator-1")
    assert settings == {
        "internal_actions": True,
        "gmail_auto_send": True,
        "calendar_holds": False,
    }


def test_string_truthy_forms_coerce_correctly(stub_profile) -> None:
    """Some supabase JSON codecs surface booleans as 'true'/'false' strings.
    agent_can must not treat 'false' as truthy."""
    stub_profile(
        babyg_agent_internal_actions="false",
        babyg_agent_gmail_auto_send="true",
    )
    assert agent_autonomy.agent_can("creator-1", "update_deal_stage") is False
    assert agent_autonomy.agent_can("creator-1", "gmail_auto_reply") is True
