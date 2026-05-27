"""Sanity tests for the babyg system prompt.

These don't assert on every word — just that the load-bearing sections
exist. They protect against accidental deletion when the prompt is
edited in the future (e.g. losing the formatting rules or the
non-negotiable safety clause).
"""

from __future__ import annotations

from app.services import prompts


def test_prompt_contains_load_bearing_sections() -> None:
    p = prompts.babyg_system_prompt()
    # Identity + role
    assert "you are babyg" in p
    assert "ai manager" in p
    # Voice + style discipline
    assert "voice:" in p
    assert "style:" in p
    # The new formatting rules that fix the word-cluster bug
    assert "formatting:" in p
    assert "blank line" in p
    assert "wall of text" in p
    # Safety + non-negotiable guarantees
    assert "safety and privacy:" in p
    assert "non-negotiable:" in p
    assert "minors:" in p
    # Tool policy survives the rewrite (product mechanics)
    assert "tool policy:" in p
    assert "create_booking" in p
    assert "read_my_profile" in p


def test_prompt_includes_creator_context() -> None:
    p = prompts.babyg_system_prompt(context={"city": "miami", "niche": "lifestyle"})
    assert "city: miami" in p
    assert "niche: lifestyle" in p


def test_prompt_injects_drafting_and_task_sections() -> None:
    p = prompts.babyg_system_prompt(draft_kind="negotiation", task_kind="offer_review")
    assert "Drafting mode:" in p
    assert "Kind: negotiation" in p
    assert "Task mode:" in p
    assert "Kind: offer_review" in p


def test_drafting_guidance_pushes_paragraphed_output_for_multi_part_drafts() -> None:
    # Negotiation + content_plan are the two cases users most often see as
    # a wall of text. Each must explicitly tell Claude to use blank lines.
    assert "blank line" in prompts.DRAFTING_GUIDANCE["negotiation"]
    assert "blank line" in prompts.DRAFTING_GUIDANCE["content_plan"]
    assert "blank line" in prompts.DRAFTING_GUIDANCE["caption"]
