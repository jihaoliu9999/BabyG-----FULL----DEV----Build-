"""Safety gates for the babyg agent's autonomous external writes.

Every autonomous send-side capability (`gmail_auto_reply` today, more
later) routes through a classifier here BEFORE the network call. The
autonomy setting on creator_profiles decides whether the capability
is even available; this module decides whether a given specific
message is boring enough to trust.

Design principles:

- **Reply-only, never first-touch.** A missing thread_id is always
  refused. The agent may only speak into a conversation the creator
  has already been part of. This alone eliminates the worst
  autonomous-send failure mode ("babyg emailed a stranger from my
  account").
- **Short.** Long messages hide commitments. Cap 500 chars.
- **No money, no numbers with $/%, no phone numbers, no URLs.** These
  are the surfaces where an autonomous message can promise
  something the creator didn't approve.
- **No committal language.** "yes", "confirmed", "i'll", "i will",
  "sounds good", "let's" — any string that implies the creator has
  agreed to something is refused.
- **Preferred patterns bias toward decline/ack.** Not required, but
  boosts confidence: "not a fit", "not right now", "pass on this",
  "received", "will review", "confirming receipt".

Every function returns a (bool, reason) tuple so the caller can log
WHY a proposal was refused into agent_cycles.tools_called.
"""

from __future__ import annotations

import re
from typing import Literal

MAX_REPLY_CHARS = 500

# Refusal patterns. Order doesn't matter but the reason string picked
# up by the caller is the FIRST match, so keep the most-informative
# reasons early.
_COMMITTAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bconfirm(?:ed|ing)?\b", re.I),
    re.compile(r"\bi(?:'|\s)?ll\b|\bi will\b|\bi can\b", re.I),
    re.compile(r"\byes(?:\.|,|!|\s)", re.I),
    re.compile(r"\bsounds good\b", re.I),
    re.compile(r"\blet(?:'|\s)?s (?:do|meet|schedule|book|talk|chat|call)\b", re.I),
    re.compile(r"\bhappy to\b", re.I),
    re.compile(r"\babsolutely\b", re.I),
    re.compile(r"\bagreed?\b", re.I),
    re.compile(r"\bdeal\b", re.I),
    re.compile(r"\bsigned?\b", re.I),
)

# Numeric / financial patterns — anything the agent shouldn't promise.
_FINANCIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"[$£€¥]\s*\d"),
    re.compile(r"\b\d+\s*%"),
    re.compile(r"\b\d+k\b", re.I),                              # 5k, 10K
    re.compile(r"\b\d{1,3}(?:,\d{3})+\b"),                       # 5,000 / 12,500
    re.compile(r"\b(?:usd|eur|gbp|cad|aud)\s*\d", re.I),
)

_URL_PATTERN = re.compile(r"https?://|\bwww\.[a-z0-9]", re.I)

_PHONE_PATTERN = re.compile(
    r"(?:\+?\d[\d\-\s\(\)]{7,}\d)"
)

# Preferred patterns — non-required, used to compute confidence when
# we want a soft signal. Callers may inspect via classify_reply.
_PREFERRED_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:not a fit|not right now|not the right fit)\b", re.I),
    re.compile(r"\bpass on this\b", re.I),
    re.compile(r"\breceived\b", re.I),
    re.compile(r"\bwill review\b", re.I),
    re.compile(r"\btake a look\b", re.I),
    re.compile(r"\bappreciate\b", re.I),
    re.compile(r"\bthanks(?:!|\.)?", re.I),
)


ReplyKind = Literal["decline", "acknowledgement", "generic_short", "unclassified"]


def is_gmail_reply_safe(
    *,
    thread_id: str | None,
    subject: str | None,
    body: str,
) -> tuple[bool, str]:
    """True iff the agent is allowed to send this specific reply.

    Refusal reasons are stable short strings so the caller can log
    them cleanly (agent_cycles.tools_called[i].outcome.reason).
    """
    # Reply-only. This is the load-bearing rule: the agent may only
    # speak into a conversation the creator already opened.
    if not thread_id or not str(thread_id).strip():
        return False, "no_thread_id_first_touch_blocked"

    text = (body or "").strip()
    if not text:
        return False, "empty_body"
    if len(text) > MAX_REPLY_CHARS:
        return False, "body_too_long"

    subject_norm = (subject or "").strip().lower()
    # Enforce reply-shaped subject. Gmail's UI defaults to "Re: ..."
    # on replies; anything else likely means the caller is starting
    # a new thread by mistake.
    if subject_norm and not subject_norm.startswith(("re:", "re :", "re[")):
        return False, "subject_not_reply_shape"

    for pattern in _COMMITTAL_PATTERNS:
        if pattern.search(text):
            return False, "committal_language"

    for pattern in _FINANCIAL_PATTERNS:
        if pattern.search(text):
            return False, "financial_content"

    if _URL_PATTERN.search(text):
        return False, "contains_url"

    if _PHONE_PATTERN.search(text):
        return False, "contains_phone"

    return True, "ok"


def classify_reply(body: str) -> ReplyKind:
    """Soft signal — bucket a safe reply for observability. Never
    used as a gate; the gate is is_gmail_reply_safe."""
    text = (body or "").strip().lower()
    if not text:
        return "unclassified"
    if any(p.search(text) for p in _PREFERRED_PATTERNS if "not " in p.pattern or "pass" in p.pattern):
        return "decline"
    if any(p.search(text) for p in _PREFERRED_PATTERNS):
        return "acknowledgement"
    if len(text) <= 200:
        return "generic_short"
    return "unclassified"
