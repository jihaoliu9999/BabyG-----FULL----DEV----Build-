"""Suggested first-turn prompts for the bot-chat empty state.

Rendered as tap-to-fill chips above the composer whenever the user's
message history is empty. Data-driven — chips that don't have real
context behind them (e.g. no unread DMs, no recent peer) drop out
rather than shipping a stub like "draft a follow-up to jihao" that
means nothing to a new user. Two evergreen chips always appear so the
row is never empty. Capped at 4.
"""

from __future__ import annotations

from typing import TypedDict


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
) -> list[BotPrompt]:
    """Return up to 4 first-turn prompts, most-context-first."""
    prompts: list[BotPrompt] = []

    if unread_dms_count > 0:
        suffix = "" if unread_dms_count == 1 else "s"
        prompts.append({
            "text": f"summarize my {unread_dms_count} unread dm{suffix}",
            "icon": _ICON_MESSAGE,
        })

    peer_first = _first_name(recent_dm_peer_name)
    if peer_first:
        prompts.append({
            "text": f"draft a follow-up to {peer_first}",
            "icon": _ICON_PENCIL,
        })

    # Evergreens — always fit; guarantee the row is never empty.
    prompts.append({"text": "what needs me today?", "icon": _ICON_CLOCK})
    prompts.append({"text": "check my week", "icon": _ICON_CALENDAR})

    return prompts[:_MAX_CHIPS]
