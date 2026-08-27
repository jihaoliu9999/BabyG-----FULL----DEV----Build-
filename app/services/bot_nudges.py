"""Proactive nudges from babyg.

Turns babyg from a passive chatbot into an ai manager that starts
conversations. Each nudge is a real assistant message inserted into the
user's bot thread, tagged with ``tool_calls.kind = "nudge"`` and a
``nudge_key`` we dedupe against so the same event never nudges twice.

Sources (all safe-to-fail — a broken source drops that nudge, not the
whole batch):

  * **new_match** — a discover card newer than the freshness gate lands
    in the viewer's feed. babyg reports it with chips ``[pitch it]`` /
    ``[see the match]`` / ``[skip]``.
  * **booking_pending** — an upcoming booking within 48h still sits at
    ``status="pending"``. babyg surfaces it with chips ``[confirm]`` /
    ``[open calendar]`` / ``[move it]``.

Trigger: ``bot_chat`` GET handler calls :func:`generate_pending` before
rendering, so a nudge lands the instant a user opens the babyg surface.
Dedupe is a Python-side lookup over the last N assistant messages —
avoids a new table, avoids a migration.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.services import babyg_deals, babyg_memory, bookings, discover
from app.services.bot import create_message, list_messages

logger = logging.getLogger(__name__)


# How far back in the thread to scan for dedupe. 60 covers ~a month of
# active use — any older nudge is already stale enough that re-firing is
# fine (the underlying entity likely changed anyway).
_DEDUPE_WINDOW = 60

# How fresh a discover card has to be before it counts as a "new" match.
# Anything older is either already-nudged (dedup catches it) or the user
# consciously skipped past — a stale nudge would just feel like spam.
_MATCH_FRESHNESS = timedelta(hours=72)

# How close to a booking's start we escalate to a nudge. Gives the user
# ± a business day of react time without pinging them a week out.
_BOOKING_HORIZON = timedelta(hours=48)

# Cap deal-derived nudges per turn so a creator with a busy pipeline
# doesn't get flooded on their first login. The dedupe pattern means
# whatever we skip today shows up on the next visit if it's still
# unresolved.
_MAX_DEAL_NUDGES = 3
_MAX_DRAFT_NUDGES = 2


def generate_pending(user_id: str) -> list[str]:
    """Insert any not-yet-nudged messages for ``user_id`` and return ids.

    Idempotent per ``nudge_key``. Empty return means either everything
    was already surfaced or nothing qualified. Category order below is
    the surfacing priority when multiple sources fire at once.
    """
    existing_keys = _recent_nudge_keys(user_id)
    inserted: list[str] = []

    candidates: list[dict[str, Any]] = []
    candidates.extend(_match_nudges(user_id))
    candidates.extend(_booking_nudges(user_id))
    candidates.extend(_connection_accepted_nudges(user_id))
    candidates.extend(_event_soon_nudges(user_id))
    candidates.extend(_pending_action_nudges(user_id))
    candidates.extend(_hot_drop_nudges(user_id))
    # Phase 8: deal-derived surfaces. Every candidate here shares its
    # dedupe_key format with the corresponding home-page card so the
    # same event never surfaces twice.
    candidates.extend(_ghosted_deal_nudges(user_id))
    candidates.extend(_late_payment_nudges(user_id))
    candidates.extend(_stale_draft_nudges(user_id))

    for nudge in candidates:
        if nudge["nudge_key"] in existing_keys:
            continue
        mid = create_message(
            user_id=user_id,
            role="assistant",
            content=nudge["content"],
            tool_calls={
                "kind": "nudge",
                "nudge_key": nudge["nudge_key"],
                "nudge_category": nudge["category"],
                "chips": nudge["chips"],
            },
        )
        if mid:
            existing_keys.add(nudge["nudge_key"])
            inserted.append(mid)
    return inserted


def _recent_nudge_keys(user_id: str) -> set[str]:
    """All nudge_keys in the recent history — the dedupe set."""
    try:
        history = list_messages(user_id, limit=_DEDUPE_WINDOW)
    except Exception:
        return set()
    keys: set[str] = set()
    for row in history:
        tc = row.get("tool_calls") or {}
        if isinstance(tc, dict) and tc.get("kind") == "nudge":
            key = tc.get("nudge_key")
            if isinstance(key, str):
                keys.add(key)
    return keys


def _match_nudges(user_id: str) -> list[dict[str, Any]]:
    """Fresh discover cards worth surfacing. Top 1 per invocation."""
    try:
        cards = discover.list_cards(
            viewer_id=user_id,
            viewer_role="creator",
            kind="all",
            limit=6,
        )
    except Exception:
        logger.exception("bot_nudges: discover.list_cards failed for %s", user_id)
        return []

    now = datetime.now(UTC)
    out: list[dict[str, Any]] = []
    for card in cards:
        created_at = _parse_iso(card.get("created_at"))
        if created_at is None or (now - created_at) > _MATCH_FRESHNESS:
            continue
        kind = card.get("card_kind") or "match"
        title = (card.get("title") or "").strip() or "a new match"
        subtitle = (card.get("subtitle") or "").strip()
        card_id = card.get("card_id") or card.get("id")
        if not card_id:
            continue
        out.append(
            {
                "nudge_key": f"new_match:{kind}:{card_id}",
                "category": "new_match",
                "content": _match_copy(kind=kind, title=title, subtitle=subtitle),
                "chips": [
                    {
                        "kind": "fill",
                        "label": "pitch it",
                        "text": f"draft a pitch for {title}",
                        "primary": True,
                    },
                    {
                        "kind": "nav",
                        "label": "see the match",
                        "href": f"/creator/discover?bring_back_kind={kind}&bring_back_id={card_id}",
                    },
                    {
                        "kind": "fill",
                        "label": "connect",
                        "text": f"send a connection request to {title}",
                    },
                ],
            }
        )
        # One per turn keeps the thread readable. Older matches will
        # surface on the next visit if they still fit the freshness gate.
        break
    return out


def _match_copy(*, kind: str, title: str, subtitle: str) -> str:
    """Manager-voice one-liner. Past-tense verb, then the ask."""
    lead = {
        "opportunity": f"**{title}** posted a brief that fits you",
        "brand": f"**{title}** looks like a fit for you",
        "creator": f"**{title}** could be worth a collab",
    }.get(kind, f"**{title}** landed in your feed")
    tail = f" — {subtitle}" if subtitle else ""
    return f"{lead}{tail}. want me to pitch?"


def _booking_nudges(user_id: str) -> list[dict[str, Any]]:
    """Pending bookings that start inside the horizon and need a decision."""
    try:
        upcoming = bookings.list_for_user(user_id, horizon="upcoming", limit=20)
    except Exception:
        logger.exception("bot_nudges: bookings.list_for_user failed for %s", user_id)
        return []

    now = datetime.now(UTC)
    horizon = now + _BOOKING_HORIZON
    out: list[dict[str, Any]] = []
    for b in upcoming:
        if (b.get("status") or "").lower() != "pending":
            continue
        starts_at = _parse_iso(b.get("starts_at"))
        if starts_at is None or starts_at > horizon:
            continue
        booking_id = b.get("id")
        if not booking_id:
            continue
        title = (b.get("title") or "").strip() or "a booking"
        when = starts_at.strftime("%a %I:%M%p").lower()
        out.append(
            {
                "nudge_key": f"booking_pending:{booking_id}",
                "category": "booking_pending",
                "content": (
                    f"your **{title}** {when} still isn't confirmed. "
                    "want me to lock it in?"
                ),
                "chips": [
                    {
                        "kind": "fill",
                        "label": "confirm it",
                        "text": f"confirm the booking for {title}",
                        "primary": True,
                    },
                    {
                        "kind": "nav",
                        "label": "open calendar",
                        "href": f"/creator/calendar/{booking_id}",
                    },
                    {
                        "kind": "fill",
                        "label": "move it",
                        "text": f"reschedule {title}",
                    },
                ],
            }
        )
    return out


def _connection_accepted_nudges(user_id: str) -> list[dict[str, Any]]:
    """Someone just accepted your connection request — babyg opens with
    a low-friction 'say hi' nudge. Reads via babyg_awareness (which
    itself reads from network + profiles with a proper public projection)
    so this stays safe-to-fail."""
    try:
        from app.services import babyg_awareness

        snap = babyg_awareness.snapshot(user_id)
    except Exception:
        logger.exception("bot_nudges: awareness snapshot failed for %s", user_id)
        return []
    accepted = snap.get("recent_connection_accepted") or {}
    peer_id = accepted.get("peer_id")
    peer_name = accepted.get("peer_name")
    if not peer_id or not peer_name:
        return []
    return [
        {
            "nudge_key": f"connection_accepted:{peer_id}",
            "category": "connection_accepted",
            "content": (
                f"**{peer_name}** just accepted your connection. "
                "want to say hi while it's fresh?"
            ),
            "chips": [
                {
                    "kind": "fill",
                    "label": f"say hi to {_first_word(peer_name).lower()}",
                    "text": f"draft a warm hello to {peer_name}",
                    "primary": True,
                },
                {
                    "kind": "nav",
                    "label": "open their profile",
                    "href": f"/creator/network/{peer_id}",
                },
                {
                    "kind": "fill",
                    "label": "pitch a collab",
                    "text": f"draft a short collab pitch for {peer_name}",
                },
            ],
        }
    ]


def _event_soon_nudges(user_id: str) -> list[dict[str, Any]]:
    """Confirmed event starting inside the next 60 min — quick check-in prompt."""
    try:
        from app.services import babyg_awareness

        snap = babyg_awareness.snapshot(user_id)
    except Exception:
        return []
    ev = snap.get("next_booking") or {}
    minutes = ev.get("minutes_until_start")
    ev_id = ev.get("id")
    title = ev.get("title")
    if not ev_id or not title or minutes is None or minutes > 60:
        return []
    venue = ev.get("venue_name") or ""
    where = f" at {venue}" if venue else ""
    return [
        {
            "nudge_key": f"event_soon:{ev_id}",
            "category": "event_soon",
            "content": (
                f"heads up — **{title}** starts in ~{minutes} min{where}. "
                "want me to send an 'on the way' note?"
            ),
            "chips": [
                {
                    "kind": "fill",
                    "label": "send \"on the way\"",
                    "text": f"draft a short 'on the way' note for {title}",
                    "primary": True,
                },
                {
                    "kind": "nav",
                    "label": "open the event",
                    "href": f"/creator/calendar/{ev_id}",
                },
                {
                    "kind": "fill",
                    "label": "reschedule",
                    "text": f"reschedule {title}",
                },
            ],
        }
    ]


def _pending_action_nudges(user_id: str) -> list[dict[str, Any]]:
    """Action proposal awaiting the user's confirm — surface it so it
    doesn't sit unnoticed. We deliberately don't include a confirm chip
    here (that stays inside the action-card in the message that
    proposed it); we just link back."""
    try:
        from app.services import babyg_awareness

        snap = babyg_awareness.snapshot(user_id)
    except Exception:
        return []
    ap = snap.get("pending_action_proposal") or {}
    ap_id = ap.get("id")
    action_type = ap.get("action_type") or ""
    if not ap_id or not action_type:
        return []
    label = action_type.replace("_", " ").replace(".", " ")
    return [
        {
            "nudge_key": f"pending_action:{ap_id}",
            "category": "pending_action",
            "content": (
                f"you have a **{label}** action waiting on your confirm. "
                "nothing runs until you tap it."
            ),
            "chips": [
                {
                    "kind": "fill",
                    "label": "open the pending action",
                    "text": "open the action i still need to confirm",
                    "primary": True,
                },
                {
                    "kind": "fill",
                    "label": "not now",
                    "text": "leave that action for later",
                },
            ],
        }
    ]


def _hot_drop_nudges(user_id: str) -> list[dict[str, Any]]:
    """Fresh intel post matching my niches — babyg flags it so the
    creator sees it in-thread instead of only on Home."""
    try:
        from app.services import babyg_awareness

        snap = babyg_awareness.snapshot(user_id)
    except Exception:
        return []
    hot = snap.get("recent_hot_drop") or {}
    hot_id = hot.get("id")
    title = hot.get("title")
    if not hot_id or not title:
        return []
    return [
        {
            "nudge_key": f"hot_drop:{hot_id}",
            "category": "hot_drop",
            "content": (
                f"new hot drop for you: **{title}**. "
                "want me to break down whether it's worth your move?"
            ),
            "chips": [
                {
                    "kind": "fill",
                    "label": "break it down",
                    "text": f"break down whether {title} is worth chasing",
                    "primary": True,
                },
                {
                    "kind": "nav",
                    "label": "open home",
                    "href": "/creator",
                },
                {
                    "kind": "fill",
                    "label": "skip",
                    "text": "skip that one",
                },
            ],
        }
    ]


def _ghosted_deal_nudges(user_id: str) -> list[dict[str, Any]]:
    """Deals that the background sweep flipped to stale_or_ghosted.
    babyg prompts a follow-up. Dedupe by deal id + calendar day so a
    creator who ignores the nudge today gets a fresh chance tomorrow
    without spam within one day."""
    try:
        deals = babyg_deals.list_deals(user_id, stage="stale_or_ghosted", limit=10)
    except Exception:
        logger.exception("bot_nudges: ghosted deal lookup failed for %s", user_id)
        return []
    day = datetime.now(UTC).strftime("%Y%m%d")
    out: list[dict[str, Any]] = []
    for deal in deals[:_MAX_DEAL_NUDGES]:
        deal_id = deal.get("id")
        brand = (deal.get("brand_name") or "").strip() or "that brand"
        if not deal_id:
            continue
        out.append(
            {
                "nudge_key": f"deal_ghosted:{deal_id}:{day}",
                "category": "deal_ghosted",
                "content": (
                    f"**{brand}** has gone quiet on the deal. "
                    "want me to draft a soft nudge or move on?"
                ),
                "chips": [
                    {
                        "kind": "fill",
                        "label": "draft a follow up",
                        "text": f"draft a short follow up to {brand}",
                        "primary": True,
                    },
                    {
                        "kind": "fill",
                        "label": "mark declined",
                        "text": f"mark the {brand} deal declined",
                    },
                    {
                        "kind": "fill",
                        "label": "give it a week",
                        "text": f"remind me about {brand} in a week",
                    },
                ],
            }
        )
    return out


def _late_payment_nudges(user_id: str) -> list[dict[str, Any]]:
    """Deals stuck in payment_pending — babyg surfaces the ones with an
    unpaid amount so the creator knows what to chase. Dedupe by deal id
    + day. Not filtered by an explicit deadline yet (deal.deadline is a
    delivery date, not a payment-by date); the fact that a deal has been
    in payment_pending long enough to matter is captured by ordering on
    last_touch_at desc plus the daily dedupe."""
    try:
        deals = babyg_deals.list_deals(user_id, stage="payment_pending", limit=10)
    except Exception:
        logger.exception("bot_nudges: late payment lookup failed for %s", user_id)
        return []
    day = datetime.now(UTC).strftime("%Y%m%d")
    out: list[dict[str, Any]] = []
    for deal in deals[:_MAX_DEAL_NUDGES]:
        deal_id = deal.get("id")
        brand = (deal.get("brand_name") or "").strip() or "that brand"
        agreed = deal.get("agreed_amount_cents")
        paid = deal.get("paid_amount_cents") or 0
        if not deal_id:
            continue
        # Skip if the deal was fully paid but stage never rolled — that
        # is a data drift we don't want to nudge the creator about.
        if agreed and paid and paid >= agreed:
            continue
        amount_note = ""
        if agreed:
            dollars = agreed // 100
            amount_note = f" — ${dollars:,} still to land"
        out.append(
            {
                "nudge_key": f"deal_payment_pending:{deal_id}:{day}",
                "category": "deal_payment_pending",
                "content": (
                    f"payment on the **{brand}** deal is still open"
                    f"{amount_note}. want me to draft the ping?"
                ),
                "chips": [
                    {
                        "kind": "fill",
                        "label": "draft the payment ping",
                        "text": f"draft a short payment reminder to {brand}",
                        "primary": True,
                    },
                    {
                        "kind": "fill",
                        "label": "mark it paid",
                        "text": f"mark the {brand} deal paid",
                    },
                ],
            }
        )
    return out


def _stale_draft_nudges(user_id: str) -> list[dict[str, Any]]:
    """Drafts the sweep flipped to status='stale'. Offer to reuse the
    body or discard. Dedupe by draft id — a stale draft nudged once
    stays acknowledged."""
    try:
        drafts = babyg_memory.list_drafts(user_id, status="stale", limit=10)
    except Exception:
        logger.exception("bot_nudges: stale draft lookup failed for %s", user_id)
        return []
    out: list[dict[str, Any]] = []
    for draft in drafts[:_MAX_DRAFT_NUDGES]:
        draft_id = draft.get("id")
        if not draft_id:
            continue
        target = (draft.get("to_addr") or "").strip()
        subject = (draft.get("subject") or "").strip()
        label = subject or (target or "an old draft")
        out.append(
            {
                "nudge_key": f"draft_stale:{draft_id}",
                "category": "draft_stale",
                "content": (
                    f"you never sent that draft **{label}**. "
                    "want me to freshen it up or bin it?"
                ),
                "chips": [
                    {
                        "kind": "fill",
                        "label": "pull it up",
                        "text": f"pull up the stale draft to {target or label}",
                        "primary": True,
                    },
                    {
                        "kind": "fill",
                        "label": "discard it",
                        "text": f"discard the stale draft to {target or label}",
                    },
                ],
            }
        )
    return out


def _first_word(text: str) -> str:
    return (text or "").strip().split(" ", 1)[0]


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        return None
