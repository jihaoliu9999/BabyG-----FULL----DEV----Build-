"""Bot chat empty-state chip prompts — evergreen chips always show;
context chips only appear when there's real data behind them."""

from __future__ import annotations

from app.services.bot_prompts import compute_prompts


def test_no_context_returns_two_evergreens() -> None:
    prompts = compute_prompts()
    assert len(prompts) == 2
    texts = [p["text"] for p in prompts]
    assert "what needs me today?" in texts
    assert "check my week" in texts


def test_unread_dms_singular_grammar() -> None:
    prompts = compute_prompts(unread_dms_count=1)
    assert prompts[0]["text"] == "summarize my 1 unread dm"
    assert prompts[0]["icon"] == "message"


def test_unread_dms_plural_grammar() -> None:
    prompts = compute_prompts(unread_dms_count=7)
    assert prompts[0]["text"] == "summarize my 7 unread dms"


def test_zero_unread_dms_skips_the_chip() -> None:
    prompts = compute_prompts(unread_dms_count=0)
    assert not any("unread" in p["text"] for p in prompts)


def test_recent_peer_uses_first_name_lowercased() -> None:
    prompts = compute_prompts(recent_dm_peer_name="Jihao Liu")
    followup = next(p for p in prompts if p["icon"] == "pencil")
    assert followup["text"] == "draft a follow-up to jihao"


def test_missing_or_blank_peer_skips_the_chip() -> None:
    for empty in (None, "", "   "):
        prompts = compute_prompts(recent_dm_peer_name=empty)
        assert not any(p["icon"] == "pencil" for p in prompts)


def test_all_context_present_returns_four_chips_max() -> None:
    prompts = compute_prompts(
        unread_dms_count=3,
        recent_dm_peer_name="Zoe Eschman",
    )
    assert len(prompts) == 4
    assert prompts[0]["text"].startswith("summarize my 3")
    assert prompts[1]["text"] == "draft a follow-up to zoe"
    assert prompts[2]["text"] == "what needs me today?"
    assert prompts[3]["text"] == "check my week"


def test_prompts_never_exceed_max_four() -> None:
    # Same call; even with future context sources added, the cap holds.
    prompts = compute_prompts(unread_dms_count=99, recent_dm_peer_name="Anna")
    assert len(prompts) <= 4


def test_every_prompt_has_a_known_icon_id() -> None:
    """Icon ids must match the switch in _partials/bot_prompt_chips.html."""
    known = {"message", "pencil", "clock", "calendar"}
    for prompts in [
        compute_prompts(),
        compute_prompts(unread_dms_count=2),
        compute_prompts(recent_dm_peer_name="Anna Doe"),
        compute_prompts(unread_dms_count=2, recent_dm_peer_name="Anna Doe"),
    ]:
        for p in prompts:
            assert p["icon"] in known, f"unknown icon: {p['icon']}"
