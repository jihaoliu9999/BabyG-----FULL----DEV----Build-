"""Tests for the agent's autonomous-send safety classifier.

These tests are load-bearing. When the agent decides to auto-send an
email on the creator's behalf, is_gmail_reply_safe is the last thing
between claude's judgement and the send button. Every refusal reason
here maps to a real failure mode we don't want in production.
"""

from __future__ import annotations

import pytest

from app.services import agent_safety

# ---- Reply-only enforcement ------------------------------------------


@pytest.mark.parametrize("thread_id", ["", None, "   "])
def test_missing_thread_id_always_refused(thread_id) -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id=thread_id, subject="Re: hi", body="thanks, will review."
    )
    assert ok is False
    assert reason == "no_thread_id_first_touch_blocked"


# ---- Basic shape gates ----------------------------------------------


def test_empty_body_refused() -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body=""
    )
    assert (ok, reason) == (False, "empty_body")


def test_long_body_refused() -> None:
    body = "x" * (agent_safety.MAX_REPLY_CHARS + 1)
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body=body
    )
    assert (ok, reason) == (False, "body_too_long")


def test_subject_without_re_refused() -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="new topic here", body="thanks, will review."
    )
    assert (ok, reason) == (False, "subject_not_reply_shape")


def test_subject_re_variants_accepted() -> None:
    for subj in ("Re: x", "RE: x", "re: x", "Re : x", "re[2]: x"):
        ok, _ = agent_safety.is_gmail_reply_safe(
            thread_id="t", subject=subj, body="thanks."
        )
        assert ok is True, subj


# ---- Committal-language gate ----------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "yes, confirmed for tuesday",
        "sounds good",
        "i'll take a look on friday",
        "i will send that over",
        "happy to hop on a call",
        "absolutely",
        "let's book that",
        "agreed on terms",
    ],
)
def test_committal_language_refused(body) -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body=body
    )
    assert ok is False
    assert reason == "committal_language"


# ---- Financial-content gate -----------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "we can pay $500 for the post",
        "our budget is 5k",
        "we can offer 25%",
        "$1,500 flat rate",
        "usd 2000 upfront",
    ],
)
def test_financial_content_refused(body) -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body=body
    )
    assert ok is False
    assert reason == "financial_content"


# ---- URL + phone refusal --------------------------------------------


def test_url_refused() -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body="see https://scam.example"
    )
    assert (ok, reason) == (False, "contains_url")


def test_phone_refused() -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body="call me at 415 555 0100 today"
    )
    assert (ok, reason) == (False, "contains_phone")


# ---- Actually-safe examples -----------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "thanks, not a fit right now.",
        "received, will review.",
        "appreciate the note.",
        "pass on this one, thanks.",
    ],
)
def test_boring_replies_pass(body) -> None:
    ok, reason = agent_safety.is_gmail_reply_safe(
        thread_id="t", subject="Re: x", body=body
    )
    assert ok is True, (body, reason)
    assert reason == "ok"


# ---- is_instagram_dm_safe ------------------------------------------


@pytest.mark.parametrize("thread_id", ["", None, "   "])
def test_ig_missing_thread_id_always_refused(thread_id) -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(
        ig_thread_id=thread_id, body="thanks!"
    )
    assert (ok, reason) == (False, "no_thread_id_first_touch_blocked")


def test_ig_empty_body_refused() -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(ig_thread_id="t", body="")
    assert (ok, reason) == (False, "empty_body")


def test_ig_long_body_refused() -> None:
    body = "x" * (agent_safety.MAX_IG_DM_CHARS + 1)
    ok, reason = agent_safety.is_instagram_dm_safe(ig_thread_id="t", body=body)
    assert (ok, reason) == (False, "body_too_long")


def test_ig_gmail_cap_would_have_passed_but_ig_cap_refuses() -> None:
    """IG cap (400) is tighter than gmail cap (500) on purpose. A body
    that would have squeaked past gmail must be refused here."""
    body = "x" * (agent_safety.MAX_IG_DM_CHARS + 10)
    assert len(body) < agent_safety.MAX_REPLY_CHARS
    ok, reason = agent_safety.is_instagram_dm_safe(ig_thread_id="t", body=body)
    assert (ok, reason) == (False, "body_too_long")


@pytest.mark.parametrize(
    "body",
    [
        "let's book that",
        "confirmed for tuesday",
        "i'll DM you tomorrow",
        "happy to hop on a call",
    ],
)
def test_ig_committal_language_refused(body) -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(ig_thread_id="t", body=body)
    assert ok is False
    assert reason == "committal_language"


@pytest.mark.parametrize(
    "body",
    [
        "we can pay $500 for the post",
        "budget is 5k",
        "we can offer 25%",
    ],
)
def test_ig_financial_content_refused(body) -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(ig_thread_id="t", body=body)
    assert ok is False
    assert reason == "financial_content"


def test_ig_url_refused() -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(
        ig_thread_id="t", body="see https://example.com"
    )
    assert (ok, reason) == (False, "contains_url")


def test_ig_phone_refused() -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(
        ig_thread_id="t", body="call 415 555 0100"
    )
    assert (ok, reason) == (False, "contains_phone")


@pytest.mark.parametrize(
    "body",
    [
        "thanks, not a fit right now.",
        "received, will review.",
        "appreciate the note.",
        "noted",
    ],
)
def test_ig_boring_replies_pass(body) -> None:
    ok, reason = agent_safety.is_instagram_dm_safe(ig_thread_id="t", body=body)
    assert ok is True, (body, reason)
    assert reason == "ok"


def test_ig_has_no_subject_gate() -> None:
    """Where gmail refuses a non-Re: subject, IG has no subject line
    at all — the classifier must not import that gate."""
    ok, reason = agent_safety.is_instagram_dm_safe(
        ig_thread_id="t", body="thanks."
    )
    assert (ok, reason) == (True, "ok")


# ---- classify_reply soft signal -------------------------------------


def test_classify_decline() -> None:
    assert agent_safety.classify_reply("not a fit for us right now.") == "decline"


def test_classify_acknowledgement() -> None:
    assert agent_safety.classify_reply("received, will review shortly.") in (
        "acknowledgement",
        "decline",  # both are preferred patterns; either bucket is fine
    )


def test_classify_generic_short() -> None:
    assert agent_safety.classify_reply("noted") == "generic_short"


def test_classify_unclassified() -> None:
    assert agent_safety.classify_reply("") == "unclassified"
