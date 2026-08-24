"""Composer chip strip for the bot chat.

Rendered as tap-to-fill chips inside the composer surround, above the
input. Chips are context-driven: the awareness snapshot decides which
four suggestions fit this exact moment (unread DM peer, freshly
accepted connection, event about to start, pending confirm, fresh
match, hot drop, open deal stage). If no live context qualifies for a
slot, the strip fills the remainder with evergreens so the row is
never empty. Always capped at exactly 4.

Tap-to-fill semantics stay identical to the legacy behavior — chip
text lands in the composer textarea, the user still taps send.
"""

from __future__ import annotations

from typing import Any, TypedDict


class BotPrompt(TypedDict):
    text: str
    icon: str


# Icon ids resolved in _partials/bot_prompt_chips.html — keep names in sync.
_ICON_MESSAGE = "message"
_ICON_PENCIL = "pencil"
_ICON_CLOCK = "clock"
_ICON_CALENDAR = "calendar"

_MAX_CHIPS = 4


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
) -> list[BotPrompt]:
    """Return up to 4 context-driven prompts for the composer chip strip.

    Legacy call-sites that only pass ``unread_dms_count`` +
    ``recent_dm_peer_name`` keep working — those two args are still
    honored. When a full awareness ``snapshot`` is provided the strip
    also considers freshly accepted connections, imminent bookings,
    pending action proposals, discover matches, and hot drops.

    Priority order (highest first):
      1. connection just accepted → nudge to say hi
      2. event starting in the next few hours → check-in / directions
      3. pending action proposal awaiting confirm → open it
      4. unread DMs → summarize / draft
      5. fresh discover match → look at the card
      6. fresh hot drop matching niches → skim it
      7. evergreens fill the rest
    """
    prompts: list[BotPrompt] = []
    snap = snapshot or {}

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

    # ---- 7. evergreens — always fit; guarantee the row is never empty ----
    prompts.append({"text": "what needs me today?", "icon": _ICON_CLOCK})
    prompts.append({"text": "check my week", "icon": _ICON_CALENDAR})

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

