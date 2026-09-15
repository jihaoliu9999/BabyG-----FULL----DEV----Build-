"""Read-only business matter aggregation for the approved Brief page.

The Brief is a consumer of persisted babyg state. It does not call
Gmail, Meta, Instagram, Calendar, Claude, or any provider API while a
page is rendering. Provider data must already be present in the local
database through existing sweeps, webhooks, action proposals, or
notifications.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from app.core import supabase_client
from app.services import action_proposals, notifications, oauth_connections

logger = logging.getLogger(__name__)

BriefSource = Literal["gmail", "instagram", "calendar", "babyg"]
BriefMatterType = Literal["deal", "response", "decision", "follow-up", "booking", "update"]

MATTER_TYPES: set[str] = {"deal", "response", "decision", "follow-up", "booking", "update"}
BRIEF_LIMIT = 20
HOME_PREVIEW_LIMIT = 3
_PRIORITY = {"urgent": 0, "high": 1, "normal": 2, "low": 3}


class _ConnectionReadError:
    pass


_CONNECTION_READ_ERROR = _ConnectionReadError()
_ConnectionResult = dict[str, Any] | None | _ConnectionReadError


def build_brief(user_id: str, *, limit: int = BRIEF_LIMIT) -> dict[str, Any]:
    """Build the real card feed for one authenticated creator."""
    connection_state = _connection_state(user_id)
    cards: list[dict[str, Any]] = []
    cards.extend(_cards_from_action_proposals(user_id, connection_state))
    cards.extend(_cards_from_notifications(user_id, connection_state))
    cards.extend(_cards_from_instagram_evaluations(user_id, connection_state))

    cards = _dedupe(cards)
    cards.sort(key=_sort_key)
    capped = cards[: max(1, min(int(limit or BRIEF_LIMIT), BRIEF_LIMIT))]
    return {
        "cards": capped,
        "empty": not bool(capped),
        "connections": connection_state,
        "has_connected_provider": any(
            connection_state.get(provider, {}).get("connected")
            for provider in ("gmail", "instagram")
        ),
    }


def home_preview_rows(user_id: str) -> list[dict[str, Any]]:
    """Return Home rows from the same matter source as /creator/brief."""
    brief = build_brief(user_id, limit=HOME_PREVIEW_LIMIT)
    return [
        {
            "slot": card["platform"],
            "source_label": card["platform_label"],
            "matter_type": card["matter_type"],
            "title": card["headline"],
            "detail": card["context"],
            "created_at": card["created_at"],
            "href": "/creator/brief",
        }
        for card in brief["cards"][:HOME_PREVIEW_LIMIT]
    ]


def _connection_state(user_id: str) -> dict[str, dict[str, Any]]:
    google = _safe_google_connection(user_id)
    instagram = _safe_instagram_connection(user_id)
    gmail_connected = bool(
        isinstance(google, dict)
        and (
            oauth_connections.google_gmail_connected(google)
            or oauth_connections.google_gmail_compose_connected(google)
            or oauth_connections.google_gmail_send_connected(google)
        )
    )
    return {
        "gmail": {
            "connected": gmail_connected,
            "error": isinstance(google, _ConnectionReadError),
        },
        "calendar": {
            "connected": bool(
                isinstance(google, dict)
                and oauth_connections.google_calendar_connected(google)
            ),
            "error": isinstance(google, _ConnectionReadError),
        },
        "instagram": {
            "connected": bool(
                isinstance(instagram, dict)
                and instagram.get("access_token")
                and instagram.get("provider_account_id")
            ),
            "error": isinstance(instagram, _ConnectionReadError),
        },
        "babyg": {"connected": True, "error": False},
    }


def _safe_google_connection(user_id: str) -> _ConnectionResult:
    try:
        return oauth_connections.get_google_connection(user_id)
    except Exception:
        logger.exception("brief.google_connection.failed user=%s", user_id)
        return _CONNECTION_READ_ERROR


def _safe_instagram_connection(user_id: str) -> _ConnectionResult:
    try:
        return oauth_connections.get_instagram_connection(user_id)
    except Exception:
        logger.exception("brief.instagram_connection.failed user=%s", user_id)
        return _CONNECTION_READ_ERROR


def _cards_from_action_proposals(
    user_id: str, connection_state: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        rows = action_proposals.list_pending_for_user(user_id=user_id, limit=BRIEF_LIMIT)
    except Exception:
        logger.exception("brief.action_proposals.failed user=%s", user_id)
        return []
    cards: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        card = _card_from_action_proposal(row, connection_state)
        if card:
            cards.append(card)
    return cards


def _card_from_action_proposal(
    row: dict[str, Any], connection_state: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    action_type = str(row.get("action_type") or "").strip()
    source = _source_from_action_type(action_type)
    if source is None:
        return None
    if source == "instagram":
        return None
    if not _source_available(source, connection_state):
        return None
    raw_preview = row.get("preview")
    preview: dict[str, Any] = raw_preview if isinstance(raw_preview, dict) else {}
    raw_payload = row.get("payload")
    payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
    headline = _first_text(
        preview.get("title"),
        preview.get("headline"),
        preview.get("summary"),
        preview.get("subject"),
    )
    if not headline:
        return None
    context = _context_from_mapping(preview) or _context_from_mapping(payload)
    matter_type = _matter_type(preview.get("matter_type"), fallback=_matter_from_action(action_type))
    return _card(
        source=source,
        matter_type=matter_type,
        headline=headline,
        context=context,
        urgent=_is_urgent(row, preview),
        created_at=row.get("created_at"),
        dedupe_key=f"proposal:{row.get('id')}",
    )


def _cards_from_notifications(
    user_id: str, connection_state: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    try:
        rows = notifications.list_for_user(user_id, limit=BRIEF_LIMIT * 2, include_archived=False)
    except Exception:
        logger.exception("brief.notifications.failed user=%s", user_id)
        return []
    cards: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        card = _card_from_notification(row, connection_state)
        if card:
            cards.append(card)
    return cards


def _card_from_notification(
    row: dict[str, Any], connection_state: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    if row.get("archived_at"):
        return None
    kind = str(row.get("kind") or "").strip()
    source = _source_from_notification(row)
    if source is None:
        return None
    if kind == "new_dm" and source != "instagram":
        return None
    if not _source_available(source, connection_state):
        return None
    headline = _first_text(row.get("title"), (row.get("metadata") or {}).get("headline"))
    if not headline:
        return None
    raw_metadata = row.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    context = _first_text(row.get("body"), metadata.get("summary"), metadata.get("context"))
    matter_type = _matter_type(metadata.get("matter_type"), fallback=_matter_from_notification(kind, source))
    return _card(
        source=source,
        matter_type=matter_type,
        headline=headline,
        context=context,
        urgent=_is_urgent(row, metadata),
        created_at=row.get("created_at"),
        dedupe_key=_notification_key(row, source),
    )


def _cards_from_instagram_evaluations(
    user_id: str, connection_state: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    if not _source_available("instagram", connection_state):
        return []
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_evaluations")
            .select("id,thread_id,message_id,creator_id,result,created_at")
            .eq("creator_id", user_id)
            .order("created_at", desc=True)
            .limit(BRIEF_LIMIT)
            .execute()
        )
    except Exception:
        logger.exception("brief.instagram_evaluations.failed user=%s", user_id)
        return []
    rows = [row for row in (getattr(result, "data", None) or []) if isinstance(row, dict)]
    if not rows:
        return []
    threads = _instagram_threads_by_id(user_id, [str(row.get("thread_id") or "") for row in rows])
    cards: list[dict[str, Any]] = []
    for row in rows:
        card = _card_from_instagram_evaluation(row, threads)
        if card:
            cards.append(card)
    return cards


def _instagram_threads_by_id(user_id: str, thread_ids: list[str]) -> dict[str, dict[str, Any]]:
    clean_ids = sorted({tid for tid in thread_ids if tid})
    if not clean_ids:
        return {}
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_threads")
            .select("id,creator_id,peer_username,ig_peer_user_id,last_message_at")
            .eq("creator_id", user_id)
            .in_("id", clean_ids)
            .execute()
        )
    except Exception:
        logger.exception("brief.instagram_threads.failed user=%s", user_id)
        return {}
    rows = [row for row in (getattr(result, "data", None) or []) if isinstance(row, dict)]
    return {str(row.get("id") or ""): row for row in rows if row.get("id")}


def _card_from_instagram_evaluation(
    row: dict[str, Any], threads: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    raw_result = row.get("result")
    result: dict[str, Any] = raw_result if isinstance(raw_result, dict) else {}
    if not _worth_responding(result):
        return None
    headline = _first_text(
        result.get("Summary"),
        result.get("summary"),
        result.get("Opportunity"),
        result.get("opportunity"),
    )
    if not headline:
        return None
    thread_id = str(row.get("thread_id") or "")
    thread = threads.get(thread_id) or {}
    peer = _instagram_peer_label(thread)
    if peer and peer.casefold() not in headline.casefold():
        headline = f"{peer}: {headline}"
    context = _first_text(
        result.get("Suggested next steps"),
        result.get("suggested_next_steps"),
        result.get("Why"),
        result.get("why"),
    )
    matter_type: BriefMatterType = "deal" if _has_opportunity(result) else "response"
    return _card(
        source="instagram",
        matter_type=matter_type,
        headline=headline,
        context=context,
        urgent=_is_urgent(row, result),
        created_at=row.get("created_at") or thread.get("last_message_at"),
        dedupe_key=f"instagram:{thread_id or row.get('message_id') or row.get('id')}",
    )


def _source_from_action_type(action_type: str) -> BriefSource | None:
    if action_type.startswith("gmail."):
        return "gmail"
    if action_type.startswith("calendar."):
        return "calendar"
    if action_type.startswith("babyg."):
        return "babyg"
    if action_type == "instagram.send_dm":
        return "instagram"
    return None


def _source_from_notification(row: dict[str, Any]) -> BriefSource | None:
    explicit = str(row.get("source_provider") or "").strip().lower()
    if explicit in {"gmail", "instagram", "calendar", "babyg"}:
        return explicit  # type: ignore[return-value]
    underlying_type = str(row.get("underlying_type") or "").strip().lower()
    link_path = str(row.get("link_path") or "").strip().lower()
    kind = str(row.get("kind") or "").strip()
    if underlying_type.startswith("instagram_") or link_path.startswith("/creator/instagram/"):
        return "instagram"
    if underlying_type.startswith("gmail_"):
        return "gmail"
    if underlying_type.startswith("calendar_") or underlying_type == "booking":
        return "calendar"
    if underlying_type.startswith("dm_"):
        return "babyg"
    if kind in {"connection_request", "profile_sync", "performance_spike", "system"}:
        return "babyg"
    if kind == "booking_reminder":
        return "calendar"
    return None


def _source_available(source: BriefSource, state: dict[str, dict[str, Any]]) -> bool:
    return bool(state.get(source, {}).get("connected"))


def _matter_from_action(action_type: str) -> BriefMatterType:
    if action_type in {"gmail.send_email", "gmail.send_draft"}:
        return "decision"
    if action_type == "gmail.create_draft":
        return "response"
    if action_type.startswith("calendar."):
        return "booking"
    if action_type == "babyg.create_booking":
        return "booking"
    if action_type == "babyg.create_content_reminder":
        return "follow-up"
    return "update"


def _matter_from_notification(kind: str, source: BriefSource) -> BriefMatterType:
    if kind == "booking_reminder" or source == "calendar":
        return "booking"
    if source == "instagram" and kind == "new_dm":
        return "response"
    if kind == "connection_request":
        return "update"
    if kind == "manager_alert":
        return "decision"
    return "update"


def _matter_type(value: Any, *, fallback: BriefMatterType) -> BriefMatterType:
    raw = str(value or "").strip().lower()
    return raw if raw in MATTER_TYPES else fallback  # type: ignore[return-value]


def _card(
    *,
    source: BriefSource,
    matter_type: BriefMatterType,
    headline: str,
    context: str,
    urgent: bool,
    created_at: Any,
    dedupe_key: str,
) -> dict[str, Any]:
    return {
        "platform": source,
        "platform_label": _source_label(source),
        "matter_type": matter_type,
        "urgent": urgent,
        "headline": _shorten(headline, 160),
        "context": _shorten(context, 180),
        "created_at": str(created_at or ""),
        "dedupe_key": dedupe_key,
    }


def _source_label(source: BriefSource) -> str:
    return {
        "gmail": "Gmail",
        "instagram": "Instagram",
        "calendar": "Calendar",
        "babyg": "babyg",
    }[source]


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None:
            continue
        text = " ".join(str(value).split()).strip()
        if text:
            return text
    return ""


def _context_from_mapping(value: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("to", "subject", "summary", "context", "body_preview", "location"):
        text = _first_text(value.get(key))
        if text and text not in parts:
            parts.append(text)
    return " · ".join(parts[:3])


def _is_urgent(row: dict[str, Any], details: dict[str, Any]) -> bool:
    if str(row.get("priority") or "").strip().lower() == "urgent":
        return True
    if str(details.get("urgent") or "").strip().lower() in {"1", "true", "yes", "urgent"}:
        return True
    urgency = _first_text(details.get("Urgency"), details.get("urgency"))
    if any(term in urgency.casefold() for term in ("urgent", "today", "tomorrow", "asap", "deadline")):
        return True
    expires_at = _parse_dt(row.get("expires_at"))
    return bool(expires_at and expires_at <= datetime.now(UTC) + timedelta(hours=24))


def _worth_responding(result: dict[str, Any]) -> bool:
    raw = _first_text(result.get("Worth responding?"), result.get("worth_responding"))
    return raw.casefold() in {"yes", "true", "worth responding", "respond"}


def _has_opportunity(result: dict[str, Any]) -> bool:
    opportunity = _first_text(result.get("Opportunity"), result.get("opportunity"))
    if not opportunity:
        return False
    low = opportunity.casefold()
    return not any(token in low for token in ("no clear", "none", "not visible"))


def _instagram_peer_label(thread: dict[str, Any]) -> str:
    username = _first_text(thread.get("peer_username"))
    if username and not username.isdigit():
        return f"@{username.lstrip('@')}"
    peer_id = _first_text(thread.get("ig_peer_user_id"))
    return f"instagram user {peer_id[-4:]}" if peer_id else ""


def _notification_key(row: dict[str, Any], source: BriefSource) -> str:
    source_thread_id = str(row.get("source_thread_id") or "").strip()
    if source_thread_id:
        return source_thread_id
    underlying_type = str(row.get("underlying_type") or "").strip()
    underlying_id = str(row.get("underlying_id") or "").strip()
    if underlying_type and underlying_id:
        return f"{source}:{underlying_type}:{underlying_id}"
    return f"notification:{row.get('id')}"


def _dedupe(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for card in cards:
        key = str(card.get("dedupe_key") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(card)
    return out


def _sort_key(card: dict[str, Any]) -> tuple[int, str]:
    priority = "urgent" if card.get("urgent") else "normal"
    return (_PRIORITY.get(priority, 2), _reverse_string(str(card.get("created_at") or "")))


def _reverse_string(value: str) -> str:
    return "".join(chr(0x10FFFF - min(ord(ch), 0x10FFFE)) for ch in value) if value else "\x00"


def _parse_dt(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _shorten(value: str, limit: int) -> str:
    text = _first_text(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."
