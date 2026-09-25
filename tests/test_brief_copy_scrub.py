"""Lock the brief-card copy scrubber and truncation caps.

Motivation: brief cards were showing raw LLM reasoning leaks like
"A human should read the full thread (all 4 unread messages) before
responding to understand the complete context..." Users saw babyg as
a thin ChatGPT wrapper. The IG evaluation prompt has been rewritten
to demand short direct copy, but LLMs regress; this test locks the
post-processor that catches the leaks that slip through and the
tighter card truncation caps (100/90 vs the old 160/180).
"""

from __future__ import annotations

import pytest

from app.services.brief import _card, _scrub_ai_leak

# ---------------------------------------------------------------------------
# 1. Sentences containing known AI-reasoning phrases are dropped.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected_kept",
    [
        # The exact leak from the user's screenshot.
        (
            "A human should read the full thread (all 4 unread messages) "
            "before responding to understand the complete context. "
            "Once the thread is reviewed, assess whether this is a fan.",
            "",
        ),
        # A useful sentence stays; the "should" one gets dropped.
        (
            "@sarah_2150 sent 4 unread — mostly fan chatter. "
            "A human should read the full thread before responding.",
            "@sarah_2150 sent 4 unread — mostly fan chatter.",
        ),
        # Multiple leaks in one blob — all dropped.
        (
            "Please review the thread. This appears to be a partnership pitch. "
            "@nike_pr wants a Q4 collab.",
            "@nike_pr wants a Q4 collab.",
        ),
        # Hedging language ("it appears that") dropped.
        (
            "It appears that this person is interested. Real signal: "
            "$8k budget mentioned.",
            "Real signal: $8k budget mentioned.",
        ),
    ],
)
def test_ai_leak_phrases_are_dropped(raw: str, expected_kept: str) -> None:
    out = _scrub_ai_leak(raw)
    if expected_kept:
        assert expected_kept in out, (
            f"expected {expected_kept!r} to survive scrubbing, got {out!r}"
        )
    for banned in (
        "should read",
        "assess whether",
        "please review",
        "it appears that",
        "this appears to be",
    ):
        assert banned not in out.casefold(), (
            f"expected {banned!r} to be scrubbed, got {out!r}"
        )


# ---------------------------------------------------------------------------
# 2. Clean, human-toned copy passes through unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "clean",
    [
        "@sarah_2150 — 4 unread, mostly fan chatter, one mentions a brand deal.",
        "Nike wants a Q4 partnership — $8k + PR gifting.",
        "Confirmed: dinner Sat 8pm at Casa D'Angelo.",
        "3 new deals in your inbox this morning.",
        "Draft ready for the Sephora holiday collab.",
        "",  # empty stays empty
    ],
)
def test_clean_copy_passes_through(clean: str) -> None:
    out = _scrub_ai_leak(clean)
    assert out == clean.strip(), (
        f"clean copy should pass through unchanged. got {out!r}"
    )


# ---------------------------------------------------------------------------
# 3. If the whole thing is leaks, return empty — the card renders with
#    source badge + handle + timestamp, which beats leaked AI text.
# ---------------------------------------------------------------------------


def test_all_leaks_returns_empty() -> None:
    all_leaks = (
        "A human should read the full thread. The user should assess. "
        "Please review the thread carefully."
    )
    out = _scrub_ai_leak(all_leaks)
    assert out == "", (
        f"expected empty when every sentence is a leak, got {out!r}"
    )


# ---------------------------------------------------------------------------
# 4. Case insensitive — mixed-case AI phrases still get caught.
# ---------------------------------------------------------------------------


def test_case_insensitive_scrub() -> None:
    out = _scrub_ai_leak(
        "SHOULD READ the entire thread. @nike wants a collab."
    )
    assert "should read" not in out.casefold()
    assert "@nike wants a collab" in out


# ---------------------------------------------------------------------------
# 5. _card() applies the scrubber and the tighter truncation caps.
# ---------------------------------------------------------------------------


def test_card_scrubs_and_truncates() -> None:
    card = _card(
        source="instagram",
        matter_type="response",
        headline=(
            "A human should read the full thread. @nike_pr wants a "
            "Q4 collab worth $8k."
        ),
        context=(
            "Please review the whole conversation before responding. "
            "$8k budget + PR gifting."
        ),
        urgent=False,
        created_at="2026-09-25T10:00:00Z",
        dedupe_key="test-1",
    )
    assert "should read" not in card["headline"].casefold()
    assert "please review" not in card["context"].casefold()
    assert "@nike_pr wants a Q4 collab" in card["headline"]
    assert "$8k budget" in card["context"]
    # Length caps: 100 headline, 90 context.
    assert len(card["headline"]) <= 100
    assert len(card["context"]) <= 90


def test_card_truncation_caps_hard_limit() -> None:
    long_text = "@nike_pr wants a Q4 collab. " * 20  # >500 chars
    card = _card(
        source="instagram",
        matter_type="deal",
        headline=long_text,
        context=long_text,
        urgent=True,
        created_at="",
        dedupe_key="test-2",
    )
    assert len(card["headline"]) <= 100
    assert len(card["context"]) <= 90
