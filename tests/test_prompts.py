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
    # The concrete example demonstrating block separation must survive
    # — examples teach the model the pattern faster than rules alone.
    assert "example of correctly-formatted response" in p
    # Safety + non-negotiable guarantees
    assert "safety and privacy:" in p
    assert "non-negotiable:" in p
    assert "minors:" in p
    # Tool policy survives the rewrite (product mechanics)
    assert "tool policy:" in p
    assert "create_booking" in p
    assert "read_my_profile" in p


def test_prompt_includes_creator_context() -> None:
    p = prompts.babyg_system_prompt(context={"location": "los angeles, california", "niche": "lifestyle"})
    assert "location: los angeles, california" in p
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


def test_prompt_contains_stats_reality_check() -> None:
    """The stats reality check is a load-bearing section: without it,
    Claude can hallucinate platform metrics or treat empty stats
    arrays as silence. Deleting it would regress the live bug."""
    p = prompts.babyg_system_prompt()
    assert "stats reality check:" in p
    # Must name the platforms it cannot read.
    assert "instagram" in p
    assert "tiktok" in p
    # Must include the fallback sentence for unsupported platforms —
    # updated to drop the dated "auto-sync will come after Meta/TikTok
    # integration" phrasing that read as a beta promise.
    assert (
        "i don't have connected post stats for that platform yet. "
        "i can work from saved performance and receipts if that helps."
    ) in p
    # Must forbid invention.
    assert "never invent numbers" in p
