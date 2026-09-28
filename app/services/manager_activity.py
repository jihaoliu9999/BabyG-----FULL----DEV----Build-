"""Activity view — real completed manager actions, grouped by day.

Powers the bot page's "Activity" sheet (top-right clock button on
/creator/bot). Reads only from action_proposals with status='executed';
those rows are the ground truth for "babyg did this in the world for
me": the creator either confirmed the proposal explicitly OR their
autonomy settings auto-confirmed it, and the executor actually ran the
external write (Gmail send, Calendar create, IG DM, native booking).

Non-executed proposals (pending / expired / cancelled / failed) are
intentionally excluded — those are decisions the user has to make or
that never landed in the world, not "activity".

No LLM calls, no external HTTP. Single indexed Supabase read per
render, keyed on (user_id, executed_at).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client

logger = logging.getLogger(__name__)

# How many days of history we keep on the Activity sheet. Anything
# older than this is not surfaced (the sheet is meant to feel like
# "recent" activity, not a permanent audit log — that lives elsewhere).
DEFAULT_LOOKBACK_DAYS = 14

# Hard cap so a chatty week never overflows the sheet. UI is bottom-
# sheet limited to ~viewport height anyway.
DEFAULT_LIMIT = 40


ActivitySource = Literal[
    "gmail",
    "instagram",
    "calendar",
    "babyg",
]


# ---------------------------------------------------------------------------
# Action-type → display metadata.
#
# `title` is the short verb phrase the user sees ("Sent email").
# `source` picks the icon slot (drives the coloured tile in the UI).
# `subtitle_fields` is the ordered list of preview keys we probe for a
# subtitle — the first one present wins. Never invent; if none of the
# fields are present we render just the title.
# ---------------------------------------------------------------------------


_ACTION_META: dict[str, dict[str, Any]] = {
    # Gmail — the send path is the strongest signal.
    "gmail.send_email": {
        "source": "gmail",
        "title": "Sent email",
        "subtitle_fields": ("subject", "to"),
    },
    "gmail.send_draft": {
        "source": "gmail",
        "title": "Sent email",
        "subtitle_fields": ("subject", "to"),
    },
    "gmail.create_draft": {
        "source": "gmail",
        "title": "Drafted reply",
        "subtitle_fields": ("subject", "to"),
    },
    # Calendar.
    "calendar.create_event": {
        "source": "calendar",
        "title": "Added to your calendar",
        "subtitle_fields": ("title", "starts_at_label", "starts_at"),
    },
    "calendar.update_event": {
        "source": "calendar",
        "title": "Updated calendar event",
        "subtitle_fields": ("title", "starts_at_label", "starts_at"),
    },
    "calendar.delete_event": {
        "source": "calendar",
        "title": "Removed calendar event",
        "subtitle_fields": ("title",),
    },
    # Instagram.
    "instagram.send_dm": {
        "source": "instagram",
        "title": "Replied to Instagram DM",
        "subtitle_fields": ("peer_username", "peer_label", "thread_id"),
    },
    # Local babyg writes.
    "create_booking": {
        "source": "babyg",
        "title": "Added a booking",
        "subtitle_fields": ("title", "when_label", "starts_at"),
    },
    "babyg.create_booking": {
        "source": "babyg",
        "title": "Added a booking",
        "subtitle_fields": ("title", "when_label", "starts_at"),
    },
    "babyg.create_content_reminder": {
        "source": "babyg",
        "title": "Set a content reminder",
        "subtitle_fields": ("title", "when_label", "starts_at"),
    },
    "babyg.create_task": {
        "source": "babyg",
        "title": "Added a task",
        "subtitle_fields": ("title", "when_label"),
    },
    "babyg.create_note": {
        "source": "babyg",
        "title": "Added a note",
        "subtitle_fields": ("title",),
    },
    "babyg.submit_creator_listing": {
        "source": "babyg",
        "title": "Updated your profile",
        "subtitle_fields": ("title",),
    },
}


def _clean_subtitle(value: Any) -> str:
    """Trim, coerce to str, cap at 90 chars. Falsy in → empty str out."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    if len(text) > 90:
        return text[:87].rstrip() + "…"
    return text


def _row_to_item(row: dict[str, Any]) -> dict[str, Any] | None:
    """Map one action_proposals row to a display item, or None if we
    don't know how to render it (e.g. an action_type we don't have
    metadata for yet — we'd rather show nothing than fake it)."""
    action_type = str(row.get("action_type") or "").strip()
    meta = _ACTION_META.get(action_type)
    if meta is None:
        return None
    preview: dict[str, Any] = row.get("preview") if isinstance(row.get("preview"), dict) else {}
    subtitle = ""
    for key in meta["subtitle_fields"]:
        subtitle = _clean_subtitle(preview.get(key))
        if subtitle:
            break
    executed_at_raw = row.get("executed_at") or row.get("updated_at") or row.get("created_at")
    return {
        "id": row.get("id"),
        "action_type": action_type,
        "source": meta["source"],
        "title": meta["title"],
        "subtitle": subtitle,
        "executed_at": executed_at_raw,
    }


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _day_label(when: datetime, *, today: date) -> str:
    """Bucket rows into 'Today' / 'Yesterday' / a compact weekday+date
    for anything older. The mockup uses 'Today' and 'Yesterday' — we
    match that exactly."""
    when_local = when.astimezone(UTC).date()
    if when_local == today:
        return "Today"
    if when_local == today - timedelta(days=1):
        return "Yesterday"
    # Older — 'Mon, Sep 22' style. Cheap and unambiguous.
    return when.strftime("%a, %b %-d") if hasattr(when, "strftime") else when.date().isoformat()


def _service():
    return supabase_client.get_service_client()


def list_recent_activity(
    user_id: str,
    *,
    days: int = DEFAULT_LOOKBACK_DAYS,
    limit: int = DEFAULT_LIMIT,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return grouped-by-day activity items for the /creator/bot Activity sheet.

    Shape:
      [
        {"label": "Today",     "items": [<item>, ...]},
        {"label": "Yesterday", "items": [<item>, ...]},
        {"label": "Mon, Sep 22", "items": [<item>, ...]},
      ]

    Only groups that actually have items are returned; an empty list
    means the sheet renders its empty state. No exception ever escapes
    this function — a supabase blip returns [] so the bot page keeps
    rendering.
    """
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=max(1, int(days)))).isoformat()
    capped = max(1, min(int(limit), 100))
    try:
        result = (
            _service()
            .table("action_proposals")
            .select(
                "id,action_type,action_category,provider,preview,"
                "executed_at,updated_at,created_at,status"
            )
            .eq("user_id", user_id)
            .eq("status", "executed")
            .gte("executed_at", since)
            .order("executed_at", desc=True)
            .limit(capped)
            .execute()
        )
    except (PostgrestAPIError, Exception):
        logger.exception("manager_activity.list_failed user=%s", user_id)
        return []
    rows = list(getattr(result, "data", None) or [])

    items: list[dict[str, Any]] = []
    for row in rows:
        item = _row_to_item(row)
        if item is None:
            continue
        items.append(item)

    today = now.astimezone(UTC).date()
    grouped: dict[str, dict[str, Any]] = {}
    ordered_labels: list[str] = []
    for item in items:
        parsed = _parse_iso(item["executed_at"])
        if parsed is None:
            continue
        label = _day_label(parsed, today=today)
        bucket = grouped.get(label)
        if bucket is None:
            bucket = {"label": label, "items": []}
            grouped[label] = bucket
            ordered_labels.append(label)
        bucket["items"].append(item)

    return [grouped[label] for label in ordered_labels if grouped[label]["items"]]


def has_new_since(
    user_id: str,
    *,
    since: datetime | None = None,
    now: datetime | None = None,
) -> bool:
    """True if there's an executed action within the last 24h that the
    activity trigger button should light up for. Never raises."""
    now = now or datetime.now(UTC)
    since_dt = since or (now - timedelta(hours=24))
    try:
        result = (
            _service()
            .table("action_proposals")
            .select("id")
            .eq("user_id", user_id)
            .eq("status", "executed")
            .gte("executed_at", since_dt.isoformat())
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("manager_activity.has_new_failed user=%s", user_id)
        return False
    rows = list(getattr(result, "data", None) or [])
    return bool(rows)
