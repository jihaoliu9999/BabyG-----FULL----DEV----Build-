"""Discovery card insights — the coral "why babyg picked this" reasons
and the freshness / signal badges that render on each card.

Everything is derived from data the ``discovery_cards`` view already
returns plus the viewer's own profile (niches, location). No new tables,
no external API calls. Fields that would need Instagram Graph API data
(precise follower count, engagement rate, "posted last week") are
intentionally not covered here — they'll slot in later when that
integration lands.

Every helper is a pure function so it's fast to test and easy to reason
about at review time. Callers pass a normalized card dict + a viewer
context dict; helpers return small typed dicts the template renders
directly. Empty lists render nothing — the card degrades gracefully
when we don't have enough data.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, TypedDict

# Icon ids resolved in the template's inline SVG switch. Keep the set in
# sync with app/templates/creator/discover.html.
_ICON_TARGET = "target"
_ICON_PIN = "pin"
_ICON_CALENDAR = "calendar"
_ICON_PLATFORM = "platform"

# Signal-badge kinds (each has its own color class in CSS).
_SIGNAL_NEW = "new"
_SIGNAL_ACTIVE = "active"
_SIGNAL_RESPONSIVE = "responsive"

# "new to babyg" window — under this many days since account creation
# counts as new. 30 balances discovery boost against not spamming the
# badge on stale accounts.
_NEW_WINDOW_DAYS = 30

# "active this week" window — under this many days since last_seen_at
# (when we have that field on the card) counts as active.
_ACTIVE_WINDOW_DAYS = 7

_MAX_REASONS = 4
_MAX_BADGES = 3


class RelevanceReason(TypedDict):
    icon: str
    text: str


class SignalBadge(TypedDict):
    kind: str  # matches CSS class (new / active / responsive)
    label: str


def _first_two(items: list[str]) -> str:
    """Return "a" for [a], "a + b" for [a, b], "a, b + one more" for 3+."""
    clean = [x for x in items if x]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} + {clean[1]}"
    return f"{clean[0]}, {clean[1]} + {len(clean) - 2} more"


def _norm_tag_list(tags: Any) -> list[str]:
    if not tags:
        return []
    return [str(t).strip().lower() for t in tags if str(t).strip()]


def relevance_reasons(
    card: dict[str, Any],
    *,
    viewer_tags: list[str] | None = None,
    viewer_location_label: str | None = None,
    viewer_platform: str | None = None,
) -> list[RelevanceReason]:
    """Return concrete reasons this card is a match — most-signal first.

    Every reason is grounded in a real DB field on either side. If none
    of the criteria hit, returns an empty list; the template treats that
    as "no derived reasons, don't render the block."
    """
    reasons: list[RelevanceReason] = []

    card_tags = _norm_tag_list(card.get("tags"))
    viewer_tags_norm = _norm_tag_list(viewer_tags)

    # 1. Niche / tag overlap — the strongest signal we have. Two matching
    #    tags is a great sign; one is still worth showing.
    overlaps = [t for t in viewer_tags_norm if t in set(card_tags)]
    if overlaps:
        reasons.append({
            "icon": _ICON_TARGET,
            "text": f"overlaps your {_first_two(overlaps)} niches",
        })

    # 2. Location match. Compare the human location_label (city, region)
    #    with a substring test — "miami, fl" matches "miami".
    if viewer_location_label and card.get("location_label"):
        v_lower = viewer_location_label.strip().lower()
        c_lower = str(card["location_label"]).strip().lower()
        if v_lower and c_lower:
            # Split on comma so "miami, fl" matches "miami" (city-only viewer).
            v_parts = [p.strip() for p in v_lower.split(",") if p.strip()]
            c_parts = [p.strip() for p in c_lower.split(",") if p.strip()]
            if any(part in c_parts for part in v_parts):
                reasons.append({
                    "icon": _ICON_PIN,
                    "text": f"based in {card['location_label']}, same as you",
                })

    # 3. Platform match — only meaningful for creator cards; brands and
    #    opportunities don't have a primary_platform we compare against.
    if (
        card.get("card_kind") == "creator"
        and card.get("primary_platform")
        and viewer_platform
        and str(card["primary_platform"]).strip().lower()
        == str(viewer_platform).strip().lower()
    ):
        reasons.append({
            "icon": _ICON_PLATFORM,
            "text": f"posts on {card['primary_platform']}, same as you",
        })

    # 4. Recent activity (deadline soon for opportunities). Compare on
    #    the calendar-date level so a deadline stamped a few microseconds
    #    in the past on the same day still reads "closes today" instead
    #    of getting swallowed by a negative timedelta.
    if card.get("card_kind") == "opportunity" and card.get("deadline"):
        try:
            deadline = datetime.fromisoformat(
                str(card["deadline"]).replace("Z", "+00:00")
            )
            days_out = (deadline.date() - datetime.now(UTC).date()).days
            if 0 <= days_out <= 14:
                if days_out == 0:
                    reasons.append({"icon": _ICON_CALENDAR, "text": "closes today"})
                elif days_out == 1:
                    reasons.append({"icon": _ICON_CALENDAR, "text": "closes tomorrow"})
                else:
                    reasons.append({
                        "icon": _ICON_CALENDAR,
                        "text": f"closes in {days_out} days",
                    })
        except (ValueError, TypeError):
            pass

    return reasons[:_MAX_REASONS]


def signal_badges(
    card: dict[str, Any], *, now: datetime | None = None
) -> list[SignalBadge]:
    """Return small freshness pills that render under the card title.

    Data-driven — a badge only appears if the underlying signal is real.
    Right now that's "new to babyg" (from created_at) and "active this
    week" (from last_seen_at when the view exposes it). "Responds fast"
    is scaffolded but only fires when the view includes an aggregate.
    """
    badges: list[SignalBadge] = []
    now = now or datetime.now(UTC)

    # "new to babyg" — signup within the last N days.
    created_at = card.get("created_at")
    if created_at:
        try:
            created = datetime.fromisoformat(
                str(created_at).replace("Z", "+00:00")
            )
            if created > now - timedelta(days=_NEW_WINDOW_DAYS):
                badges.append({"kind": _SIGNAL_NEW, "label": "new to babyg"})
        except (ValueError, TypeError):
            pass

    # "active this week" — last_seen_at within the last N days. The view
    # doesn't emit last_seen_at today; when it does, this fires. We check
    # gracefully so absent data means no badge, not a crash.
    last_seen = card.get("last_seen_at")
    if last_seen:
        try:
            seen = datetime.fromisoformat(
                str(last_seen).replace("Z", "+00:00")
            )
            if seen > now - timedelta(days=_ACTIVE_WINDOW_DAYS):
                badges.append({"kind": _SIGNAL_ACTIVE, "label": "active this week"})
        except (ValueError, TypeError):
            pass

    # "responds fast" — hits when the view (later) exposes a DM-response
    # aggregate. Threshold: median response under 24h.
    responds = card.get("dm_median_response_hours")
    if isinstance(responds, int | float) and 0 < responds <= 24:
        badges.append({"kind": _SIGNAL_RESPONSIVE, "label": "responds fast"})

    return badges[:_MAX_BADGES]
