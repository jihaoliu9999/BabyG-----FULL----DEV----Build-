"""Composer chip strip for the bot chat.

Rendered as tap-to-run chips inside the composer surround, above the
input. Chips are context-driven: the current conversation ranks first,
then awareness snapshot signals (unread DMs, accepted connection,
event about to start, pending confirm, fresh match, hot drop, open
deal stage). Cold opens backfill from a rotating evergreen pool.

The strip is capped at three useful mobile controls so it feels like a
real assistant toolbar, not a cramped row of tiny pills.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any, TypedDict


class BotPrompt(TypedDict, total=False):
    text: str
    icon: str
    # Optional verb-weight for coloring the chip. "primary" for the next
    # concrete move, "warn" for review-before-action, "good" for a
    # confirm/positive close. Omitted for evergreens.
    tone: str
    # When True bot.js auto-submits the chip on tap instead of filling
    # the composer. Reserved for confirm-style verbs (approving a
    # pending action, sending a staged draft) where a one-tap gesture
    # is the whole point.
    submit: bool


# Icon ids resolved in _partials/bot_prompt_chips.html — keep names in sync.
_ICON_MESSAGE = "message"
_ICON_PENCIL = "pencil"
_ICON_CLOCK = "clock"
_ICON_CALENDAR = "calendar"

_MAX_CHIPS = 3

# Rotating pool of concrete manager questions. Each one maps to a
# real tool call the ai can act on (read_my_dms, read_my_deals,
# read_my_drafts, read_my_calendar, read_my_gmail). No vague
# "what's my next move" / "read me the room" filler — every chip
# is something a creator would actually tap. Rotation is keyed to
# the current UTC hour so a burst of opens stays consistent while
# the set refreshes across the day.
_ROTATING_PROMPTS: list[BotPrompt] = [
    {"text": "who needs a reply today?", "icon": _ICON_MESSAGE},
    {"text": "what deals are quiet?", "icon": _ICON_CLOCK},
    {"text": "any brands owe me money?", "icon": _ICON_CLOCK},
    {"text": "pull up my open drafts", "icon": _ICON_PENCIL},
    {"text": "what's on my calendar today?", "icon": _ICON_CALENDAR},
    {"text": "who paid me recently?", "icon": _ICON_CLOCK},
    {"text": "any unread dms i missed?", "icon": _ICON_MESSAGE},
    {"text": "show me my pipeline", "icon": _ICON_MESSAGE},
    {"text": "what should i post today?", "icon": _ICON_PENCIL},
    {"text": "any old drafts worth sending?", "icon": _ICON_PENCIL},
    {"text": "check my gmail for brand replies", "icon": _ICON_MESSAGE},
]


def _first_name(full_name: str | None) -> str | None:
    """Return the lowercased first token of ``full_name`` or None."""
    if not full_name:
        return None
    token = full_name.strip().split(" ", 1)[0].strip()
    return token.lower() or None


def compute_prompts(
    *,
    unread_dms_count: int = 0,
    recent_dm_peer_name: str | None = None,
    snapshot: dict[str, Any] | None = None,
    messages: list[dict[str, Any]] | None = None,
    session_seed: str | None = None,
) -> list[BotPrompt]:
    """Return up to 3 context-driven prompts for the composer chip strip.

    Live conversation state ranks above the awareness snapshot: if the
    last assistant message has a pending action proposal, the row
    collapses to confirm / review / cancel verbs — that is the only
    move worth showing until the creator resolves it. If the reply
    just staged a brand-specific move, chips point at that brand.

    ``messages`` is the ordered thread with tool_calls attached; the
    last assistant row is inspected for `tool_calls.status == 'pending'`
    and the associated action_type / payload. Evergreens ("what needs
    me today?", "check my week") only fire on a genuine cold open —
    no user messages yet. Live turns get category-specific suggestions
    inferred from the recent conversation.

    Legacy call-sites that only pass ``unread_dms_count`` +
    ``recent_dm_peer_name`` keep working; those two args are still
    honored.

    Priority order (highest first):
      0. pending action proposal on the last assistant turn (verb chips)
      1. brand-specific next move from the last staged action
      2. topic-specific next moves from the conversation
      3. connection just accepted -> nudge to say hi
      4. event starting in the next few hours -> check-in / directions
      5. pending action proposal (awareness snapshot fallback)
      6. unread DMs -> summarize / draft
      7. fresh discover match -> look at the card
      8. fresh hot drop matching niches -> skim it
      9. evergreens ONLY on cold open
    """
    prompts: list[BotPrompt] = []
    snap = snapshot or {}
    msgs = messages or []
    rotation_offset = _seeded_offset(session_seed)

    # ---- 0. pending action on the last assistant turn ----
    #
    # If the newest assistant message staged an external write, the
    # only chips worth showing are the verbs that resolve it: confirm,
    # review, cancel. That collapses the row and beats every other
    # signal — the creator has ONE decision in front of them.
    pending = _last_pending_action(msgs)
    if pending is not None:
        brand = _brand_hint_from_action(pending)
        review_text = (
            f"read the draft to {brand} back to me"
            if brand
            else "read the draft back to me before we send"
        )
        return [
            {
                "text": "looks good, send it",
                "icon": _ICON_MESSAGE,
                "tone": "good",
                "submit": True,
            },
            {
                "text": review_text,
                "icon": _ICON_PENCIL,
                "tone": "warn",
            },
            {
                "text": "cancel it",
                "icon": _ICON_CLOCK,
                "tone": "primary",
            },
        ]

    # ---- 1. brand-specific move pulled from the most recent assistant turn ----
    brand_from_reply = _last_brand_mentioned(msgs)
    if brand_from_reply:
        prompts.append({
            "text": f"draft a counter to {brand_from_reply}",
            "icon": _ICON_PENCIL,
            "tone": "primary",
        })
        prompts.append({
            "text": f"pull the {brand_from_reply} thread",
            "icon": _ICON_MESSAGE,
        })
        prompts.append({
            "text": f"remind me about {brand_from_reply} in 2 days",
            "icon": _ICON_CLOCK,
        })

    # ---- 2. conversation-specific suggestions ----
    #
    # These keep the chip row alive after a real chat turn. We avoid
    # quoting raw user text in chip labels; category intent is enough
    # and safer on a small screen.
    if not brand_from_reply:
        prompts.extend(_conversation_prompts(msgs, offset=rotation_offset))

    # ---- 1. connection accepted (highest-signal social moment) ----
    accepted = snap.get("recent_connection_accepted") or {}
    accepted_first = _first_name(accepted.get("peer_name"))
    if accepted_first:
        prompts.append({
            "text": f"say hi to {accepted_first}",
            "icon": _ICON_MESSAGE,
        })

    # ---- 2. event starting soon (<4h) ----
    next_book = snap.get("next_booking") or {}
    minutes = next_book.get("minutes_until_start")
    booking_title = (next_book.get("title") or "").strip().lower() if next_book else ""
    if minutes is not None and minutes < 240 and booking_title:
        prompts.append({
            "text": f"send a check-in for {booking_title}",
            "icon": _ICON_CALENDAR,
        })

    # ---- 3. pending action awaiting confirm ----
    ap = snap.get("pending_action_proposal") or {}
    if ap.get("action_type"):
        prompts.append({
            "text": "open the action i still need to confirm",
            "icon": _ICON_CLOCK,
        })

    # ---- 4. unread DMs (legacy signal path, honored either way) ----
    unread_count = int(unread_dms_count or (snap.get("unread_dms") or {}).get("count") or 0)
    if unread_count > 0:
        suffix = "" if unread_count == 1 else "s"
        prompts.append({
            "text": f"summarize my {unread_count} unread dm{suffix}",
            "icon": _ICON_MESSAGE,
        })

    # ---- 4b. draft follow-up to the most recent DM peer ----
    peer_first = _first_name(recent_dm_peer_name) or _first_name(
        (snap.get("unread_dms") or {}).get("latest_peer_name")
    )
    if peer_first and not accepted_first:
        # Skip if we already have a "say hi to <accepted peer>" chip —
        # two "reach out to a person" chips crowd the row.
        prompts.append({
            "text": f"draft a follow-up to {peer_first}",
            "icon": _ICON_PENCIL,
        })

    # ---- 5. fresh discover match ----
    fresh = snap.get("fresh_discover_match") or {}
    fresh_title = (fresh.get("title") or "").strip().lower()
    if fresh_title:
        prompts.append({
            "text": f"look at {fresh_title}",
            "icon": _ICON_PENCIL,
        })

    # ---- 6. hot drop matching niches ----
    hot = snap.get("recent_hot_drop") or {}
    if hot.get("title"):
        prompts.append({
            "text": "show me the new hot drop",
            "icon": _ICON_CLOCK,
        })

    # ---- 9. rotating backfill ----
    #
    # Cold opens get three fresh manager questions. If a session seed is
    # available, it comes from the signed login cookie, so a new login
    # can rotate the choices without persisting any extra database row.
    if not _has_user_turn(msgs):
        prompts.extend(_rotating_backfill(offset=rotation_offset))

    # Dedupe by text so the same suggestion doesn't slot twice from
    # different signals (e.g. accepted peer == latest DM peer).
    seen: set[str] = set()
    unique: list[BotPrompt] = []
    for p in prompts:
        if p["text"] in seen:
            continue
        seen.add(p["text"])
        unique.append(p)
        if len(unique) >= _MAX_CHIPS:
            break
    return unique



# ---- Turn-aware helpers ---------------------------------------------------


def _has_user_turn(messages: list[dict[str, Any]]) -> bool:
    """True iff the thread has at least one user message. Guards the
    evergreen chips: they should only show on a genuine cold open."""
    return any(
        str(msg.get("role") or "").lower() == "user" for msg in messages or []
    )


def _last_pending_action(messages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the tool_calls dict of the most recent assistant message
    that is still awaiting the creator's confirm, or None. Only the
    LAST assistant message counts — an older pending action that a
    newer turn has already resolved must not resurrect chips."""
    if not messages:
        return None
    for msg in reversed(messages):
        role = str(msg.get("role") or "").lower()
        if role != "assistant":
            continue
        tc = msg.get("tool_calls")
        if not isinstance(tc, dict):
            return None
        if tc.get("kind") != "proposed_action":
            return None
        if str(tc.get("status") or "") != "pending":
            return None
        return tc
    return None


def _brand_hint_from_action(pending: dict[str, Any]) -> str | None:
    """Given a pending proposed_action, return the recipient hint for a
    chip label. For gmail actions we use the local-part of `to` when
    it looks brand-ish; otherwise we skip so the chip stays generic."""
    payload = pending.get("payload") if isinstance(pending, dict) else None
    if not isinstance(payload, dict):
        return None
    to = str(payload.get("to") or "").strip().lower()
    if not to or "@" not in to:
        return None
    local, _, domain = to.partition("@")
    # A friendly first token: "team@vans.example" -> "vans",
    # "anna@olipop.co" -> "olipop". Prefer the domain root over the
    # local part because local parts are often personal names.
    root = (domain.split(".", 1)[0] or "").strip()
    if root and root not in {"gmail", "outlook", "hotmail", "yahoo", "icloud", "proton"}:
        return root
    # Personal-mail domain: the local part is what we know.
    return local or None


_STOPWORD_TOKENS = frozenset({
    "the", "and", "you", "your", "with", "that", "this", "are", "for",
    "our", "have", "has", "was", "were", "will", "would", "could",
    "should", "just", "then", "than", "them", "from", "into", "onto",
    "about", "before", "after", "still", "want", "need", "yeah",
    "drafting", "drafted", "staged", "sent", "morning", "evening",
    "tonight", "today", "tomorrow", "verdict",
})


def _last_brand_mentioned(messages: list[dict[str, Any]]) -> str | None:
    """Extract a brand-ish token from the most recent assistant message,
    falling back to None. The heuristic is deliberately narrow:

        * only inspect the last assistant message
        * skip if that message has a pending action (handled above)
        * look for a bolded token (**Vans**) — the prompt already tells
          babyg to bold brand names, so this is the most reliable
          signal we have without an NER pass
        * otherwise pull the first capitalized token that isn't a
          stopword

    We only surface chips off this signal when the token is clearly a
    brand-ish word (not "I", "You", "Monday" etc.). This is a
    heuristic — a wrong chip is cheap because the creator can ignore
    it, but a wrong CONFIRM chip would be dangerous, which is why
    those come from the pending-action path instead.
    """
    if not messages:
        return None
    for msg in reversed(messages):
        role = str(msg.get("role") or "").lower()
        if role != "assistant":
            continue
        content = str(msg.get("content") or "")
        return _extract_brand_from_text(content)
    return None


def _extract_brand_from_text(text: str) -> str | None:
    # 1. Prefer **bold** tokens — the prompt uses them for brand names.
    bold_match = re.search(r"\*\*([A-Z][A-Za-z0-9&\-]{1,40})\*\*", text)
    if bold_match:
        return bold_match.group(1).lower()
    # 2. Fall back: a capitalized token near the start of a sentence
    #    that isn't a stopword and isn't a day/month.
    day_words = frozenset({
        "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
        "Saturday", "Sunday", "January", "February", "March", "April",
        "May", "June", "July", "August", "September", "October",
        "November", "December",
    })
    for token in re.findall(r"\b([A-Z][A-Za-z0-9&\-]{2,40})\b", text):
        if token in day_words:
            continue
        if token.lower() in _STOPWORD_TOKENS:
            continue
        return token.lower()
    return None


_ACK_ONLY_TOKENS = frozenset({
    "ok",
    "okay",
    "yes",
    "yeah",
    "yep",
    "no",
    "nah",
    "thanks",
    "thank",
    "cool",
    "done",
    "sent",
})

_CONVERSATION_PROMPT_SETS: dict[str, list[BotPrompt]] = {
    "dm": [
        {"text": "draft the clean reply", "icon": _ICON_PENCIL, "tone": "primary"},
        {"text": "summarize the thread first", "icon": _ICON_MESSAGE},
        {"text": "pull the relationship context", "icon": _ICON_MESSAGE},
        {"text": "make it sound more human", "icon": _ICON_PENCIL},
    ],
    "deal": [
        {"text": "draft the next deal move", "icon": _ICON_PENCIL, "tone": "primary"},
        {"text": "pull the deal context", "icon": _ICON_MESSAGE},
        {"text": "tighten the pitch", "icon": _ICON_PENCIL},
        {"text": "check what they owe me", "icon": _ICON_CLOCK},
    ],
    "content": [
        {"text": "turn this into a post plan", "icon": _ICON_PENCIL, "tone": "primary"},
        {"text": "write the caption", "icon": _ICON_PENCIL},
        {"text": "shape it for reels", "icon": _ICON_MESSAGE},
        {"text": "make a content angle", "icon": _ICON_CALENDAR},
    ],
    "calendar": [
        {"text": "check my week around this", "icon": _ICON_CALENDAR},
        {"text": "make this a plan", "icon": _ICON_PENCIL, "tone": "primary"},
        {"text": "find the open window", "icon": _ICON_CLOCK},
        {"text": "turn it into a booking", "icon": _ICON_CALENDAR},
    ],
    "places": [
        {"text": "map this into a plan", "icon": _ICON_CALENDAR, "tone": "primary"},
        {"text": "rank the best options", "icon": _ICON_MESSAGE},
        {"text": "turn it into an itinerary", "icon": _ICON_PENCIL},
        {"text": "save the strongest picks", "icon": _ICON_CLOCK},
    ],
    "generic": [
        {"text": "go deeper on this", "icon": _ICON_MESSAGE},
        {"text": "turn this into next steps", "icon": _ICON_PENCIL, "tone": "primary"},
        {"text": "make it more direct", "icon": _ICON_PENCIL},
        {"text": "pull useful context first", "icon": _ICON_MESSAGE},
    ],
}


def _conversation_prompts(
    messages: list[dict[str, Any]], *, offset: int
) -> list[BotPrompt]:
    text = _last_user_text(messages)
    if text is None:
        return []
    normalized = " ".join(text.lower().split())
    if not normalized:
        return []
    tokens = set(re.findall(r"[a-z0-9']+", normalized))
    if tokens and tokens <= _ACK_ONLY_TOKENS:
        return []
    if len(normalized) < 8:
        return []

    category = _conversation_category(normalized)
    return _pick_chips(_CONVERSATION_PROMPT_SETS[category], offset=offset)


def _last_user_text(messages: list[dict[str, Any]]) -> str | None:
    for msg in reversed(messages or []):
        if str(msg.get("role") or "").lower() == "user":
            return str(msg.get("content") or "")
    return None


def _conversation_category(text: str) -> str:
    if _has_any(text, ("dm", "reply", "message", "thread", "text back", "inbox")):
        return "dm"
    if _has_any(text, (
        "brand",
        "deal",
        "rate",
        "price",
        "counter",
        "offer",
        "contract",
        "paid",
        "payment",
        "pitch",
        "sell",
        "sponsor",
        "collab",
    )):
        return "deal"
    if _has_any(text, (
        "post",
        "caption",
        "reel",
        "story",
        "content",
        "tiktok",
        "youtube",
        "instagram",
        "video",
    )):
        return "content"
    if _has_any(text, (
        "calendar",
        "week",
        "today",
        "tomorrow",
        "schedule",
        "meeting",
        "call",
        "book",
    )):
        return "calendar"
    if _has_any(text, (
        "where",
        "restaurant",
        "bar",
        "club",
        "dinner",
        "night",
        "miami",
        "place",
        "lounge",
    )):
        return "places"
    return "generic"


def _has_any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _pick_chips(pool: list[BotPrompt], *, offset: int) -> list[BotPrompt]:
    if not pool:
        return []
    rotated = _rotate(pool, offset=offset)
    return rotated[:_MAX_CHIPS]


def _rotate(pool: list[BotPrompt], *, offset: int) -> list[BotPrompt]:
    if not pool:
        return []
    o = offset % len(pool)
    return list(pool[o:]) + list(pool[:o])


def _hour_offset() -> int:
    """Rotation index derived from the current UTC hour. Same hour
    yields the same offset (so a burst of opens stays visually
    consistent), a new hour rotates the pool so chips refresh across
    the day. Isolated as a helper so tests can monkeypatch it."""
    return datetime.now(UTC).hour


def _seeded_offset(session_seed: str | None) -> int:
    if not session_seed:
        return _hour_offset()
    digest = hashlib.blake2s(session_seed.encode("utf-8"), digest_size=2).digest()
    return int.from_bytes(digest, "big")


def _rotating_backfill(*, offset: int) -> list[BotPrompt]:
    """Return the pool rotated by `offset`. Callers upstream dedupe
    against already-inserted signal chips and then trim to _MAX_CHIPS,
    so we can safely return the full pool and let the dedupe cap take
    care of the rest."""
    return _rotate(_ROTATING_PROMPTS, offset=offset)
