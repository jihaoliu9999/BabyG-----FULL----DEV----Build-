"""babyg's system prompt is grounded to the user's real device time.

Without this, babyg falls back to training-time hallucinations
("today is July 3, 2025" when it's actually August 2026). These tests
lock the three-layer provenance chain:

  1. When the client sends user_now_iso + user_tz on submit, the
     system prompt reflects that exact wall-clock time and tz.
  2. When the client sends nothing, the prompt falls back to UTC.
  3. Profile city + region always land in the prompt if present.
"""

from __future__ import annotations

import pytest

from app.services import bot as bot_module


@pytest.fixture()
def stub_bot_writes(monkeypatch):
    """Cut every downstream side-effect so we can inspect the prompt."""
    inserted: list[dict] = []
    monkeypatch.setattr(bot_module, "create_message",
                        lambda **kw: inserted.append(kw) or "m-1")
    monkeypatch.setattr(bot_module, "list_messages", lambda uid, limit=100: [])
    monkeypatch.setattr(bot_module, "_scope_flag", lambda c: None)
    monkeypatch.setattr(bot_module, "_should_use_agent_for_action", lambda c: True)
    return inserted


def _capture_prompt(monkeypatch, *, user_now_iso=None, user_tz=None,
                    profile=None):
    captured: dict[str, str] = {}

    def _fake_agent_loop(*, user_id, system_prompt, messages):
        captured["prompt"] = system_prompt
        # Minimal ClaudeResponse shim.
        class _R:
            text = "ok"
            input_tokens = 0
            output_tokens = 0
        return _R(), [], None

    monkeypatch.setattr(bot_module, "_run_agent_loop", _fake_agent_loop)
    if profile is not None:
        monkeypatch.setattr(bot_module.profiles, "get_creator_profile",
                            lambda uid: profile)
    else:
        monkeypatch.setattr(bot_module.profiles, "get_creator_profile",
                            lambda uid: {})

    bot_module.handle_creator_message(
        user_id="u-1",
        content="what should I post today?",
        user_now_iso=user_now_iso,
        user_tz=user_tz,
    )
    return captured["prompt"]


# ---------------------------------------------------------------------------


def test_prompt_carries_device_datetime_and_timezone(monkeypatch, stub_bot_writes):
    """When the client sends both, babyg's system prompt names the
    exact wall-clock time in that timezone."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="2026-08-19T13:47:00-04:00",
        user_tz="America/New_York",
    )
    # Human-formatted today line lands in the "creator context:" block.
    assert "today:" in prompt
    assert "wednesday" in prompt  # Aug 19, 2026 is a Wednesday
    assert "aug 19, 2026" in prompt
    assert "1:47pm" in prompt
    assert "america/new_york" in prompt.lower()


def test_prompt_falls_back_to_utc_when_client_sends_nothing(
    monkeypatch, stub_bot_writes
):
    """No JS = no device stamp. The prompt still carries *a* today
    line (server UTC) rather than dropping the field entirely."""
    prompt = _capture_prompt(monkeypatch, user_now_iso=None, user_tz=None)
    assert "today:" in prompt
    assert "(utc)" in prompt.lower()


def test_prompt_includes_profile_location(monkeypatch, stub_bot_writes):
    """Miami + FL from profile land in the prompt so babyg answers with
    local context by default."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="2026-08-19T13:47:00-04:00",
        user_tz="America/New_York",
        profile={"location_city": "Miami", "location_region": "FL"},
    )
    assert "location:" in prompt
    assert "Miami, FL" in prompt


def test_prompt_omits_location_when_profile_empty(monkeypatch, stub_bot_writes):
    """No stored location = no false hint in the prompt."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="2026-08-19T13:47:00-04:00",
        user_tz="America/New_York",
        profile={},
    )
    assert "location:" not in prompt


def test_prompt_survives_malformed_iso(monkeypatch, stub_bot_writes):
    """A garbage user_now_iso (crafted or corrupted) falls back to UTC
    rather than blowing up the whole turn."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="not-a-real-datetime",
        user_tz="America/New_York",
    )
    assert "today:" in prompt
    # Malformed iso → UTC fallback label wins over the client tz.
    assert "(utc)" in prompt.lower()


# ---------------------------------------------------------------------------
# Timezone conversion — regression tests for the "babyg is a day ahead" bug
# where bot.js sent `new Date().toISOString()` (always UTC) and the server
# formatted the UTC wall-clock while labeling it with the user's tz string,
# so a creator in LA at 8:30 PM PDT saw babyg claim it was the next day.
# ---------------------------------------------------------------------------


def test_utc_iso_from_device_is_converted_into_user_tz(
    monkeypatch, stub_bot_writes
):
    """The core hallucination: bot.js sends the UTC-normalized form
    (`2026-08-27T03:30:00Z`) with tz `America/Los_Angeles`. The prompt
    must reflect the LA wall clock (Wed Aug 26, 8:30 pm), NOT the UTC
    calendar (Thu Aug 27, 3:30 am)."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="2026-08-27T03:30:00Z",
        user_tz="America/Los_Angeles",
    )
    assert "wednesday" in prompt
    assert "aug 26, 2026" in prompt
    assert "8:30pm" in prompt
    assert "america/los_angeles" in prompt.lower()
    # Regression: the UTC calendar day must NOT leak into the prompt.
    assert "thursday" not in prompt
    assert "aug 27, 2026" not in prompt


def test_utc_iso_with_bad_tz_string_falls_back_to_utc_label(
    monkeypatch, stub_bot_writes
):
    """Client sent a valid UTC ISO but a garbage tz string. We keep
    the (UTC) label rather than mislabeling and never raise."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="2026-08-27T03:30:00Z",
        user_tz="Not/AZone",
    )
    assert "today:" in prompt
    assert "(utc)" in prompt.lower()


def test_offset_iso_still_reads_local_wall_clock(
    monkeypatch, stub_bot_writes
):
    """When the client sends an ISO with an explicit offset (older
    clients did this), converting into user_tz must NOT double-shift.
    2026-08-19 13:47 -04:00 in America/New_York is still 1:47 pm."""
    prompt = _capture_prompt(
        monkeypatch,
        user_now_iso="2026-08-19T13:47:00-04:00",
        user_tz="America/New_York",
    )
    assert "wednesday" in prompt
    assert "aug 19, 2026" in prompt
    assert "1:47pm" in prompt
