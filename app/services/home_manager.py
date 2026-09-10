"""Read-side assembler for Creator Home V2.

Home is a briefing, not a dashboard. This module takes already-persisted
product state and shapes it into a small set of ranked sections. It does
not call Meta, Google, Anthropic, Tavily, or any other external provider.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import supabase_client
from app.services import notifications

logger = logging.getLogger(__name__)

_PRIORITY_SCORE = {"urgent": 400, "high": 300, "normal": 200, "low": 100}
_TERMINAL_DEAL_STAGES = {"paid", "declined", "cancelled"}


def latest_sweep_run(user_id: str) -> dict[str, Any] | None:
    """Most recent background sweep row for this creator.

    Reads `bot_job_runs`, which is the Railway cron idempotence/audit log.
    Empty means we have no proof a sweep has run for this creator yet.
    """
    try:
        result = (
            supabase_client.get_service_client()
            .table("bot_job_runs")
            .select("job_name,ran_at,outcome,detail")
            .eq("target_user_id", user_id)
            .order("ran_at", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("home_manager.latest_sweep_run.failed user=%s", user_id)
        return None
    rows = list(getattr(result, "data", None) or [])
    return rows[0] if rows else None


def open_deal_count(user_id: str) -> int:
    """Count non-terminal BabyG memory deals watched for this creator."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("babyg_memory_deals")
            .select("id,stage")
            .eq("creator_id", user_id)
            .execute()
        )
    except Exception:
        logger.exception("home_manager.open_deal_count.failed user=%s", user_id)
        return 0
    rows = list(getattr(result, "data", None) or [])
    return sum(
        1
        for row in rows
        if str(row.get("stage") or "").lower() not in _TERMINAL_DEAL_STAGES
    )


def build(
    *,
    user_id: str,
    google_connection: dict[str, Any] | None,
    instagram_connection: dict[str, Any] | None,
    instagram_snapshot: dict[str, Any] | None,
    instagram_growth: dict[str, int | None] | None,
    latest_agent_cycle: dict[str, Any] | None,
    latest_sweep: dict[str, Any] | None,
    manager_activity: list[dict[str, Any]],
    unread_notifs: list[dict[str, Any]],
    pending_actions: list[dict[str, Any]],
    pending_connections: list[dict[str, Any]],
    upcoming_bookings: list[dict[str, Any]],
    matched_picks: list[dict[str, Any]],
    overnight_recap: dict[str, Any] | None,
    ig_dm_unread_count: int,
    open_deals: int,
    calendar_connected: bool,
    gmail_connected: bool,
) -> dict[str, Any]:
    """Return the complete Home V2 view-model."""
    status = _manager_status(
        google_connection=google_connection,
        instagram_connection=instagram_connection,
        instagram_snapshot=instagram_snapshot,
        latest_agent_cycle=latest_agent_cycle,
        latest_sweep=latest_sweep,
        calendar_connected=calendar_connected,
        gmail_connected=gmail_connected,
    )
    needs_you = _needs_you(
        manager_activity=manager_activity,
        unread_notifs=unread_notifs,
        pending_actions=pending_actions,
        pending_connections=pending_connections,
        upcoming_bookings=upcoming_bookings,
        instagram_connection=instagram_connection,
        ig_dm_unread_count=ig_dm_unread_count,
    )
    if not needs_you and status.get("tone") == "attention":
        needs_you.append(_status_attention_item(status))
    brief_candidates = _brief(
        manager_activity=manager_activity,
        matched_picks=matched_picks,
        instagram_growth=instagram_growth or {},
        already_used_ids={str(item.get("id")) for item in needs_you if item.get("id")},
    )
    primary_focus = needs_you[0] if needs_you else (brief_candidates[0] if brief_candidates else None)
    primary_id = str((primary_focus or {}).get("id") or "")
    brief = [
        item
        for item in brief_candidates
        if str(item.get("id") or "") != primary_id
    ]
    return {
        "status": status,
        "needs_you": needs_you[:3],
        "primary_focus": primary_focus,
        "brief": brief[:3],
        "today": _today(upcoming_bookings, calendar_connected=calendar_connected),
        "handled": _handled(overnight_recap),
        "watching": _watching(
            open_deals=open_deals,
            opportunity_count=len(matched_picks or []),
            upcoming_count=len(upcoming_bookings or []),
            ig_dm_unread_count=ig_dm_unread_count,
        ),
        "clear": primary_focus is None,
    }


def _manager_status(
    *,
    google_connection: dict[str, Any] | None,
    instagram_connection: dict[str, Any] | None,
    instagram_snapshot: dict[str, Any] | None,
    latest_agent_cycle: dict[str, Any] | None,
    latest_sweep: dict[str, Any] | None,
    calendar_connected: bool,
    gmail_connected: bool,
) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []

    if latest_agent_cycle:
        status = str(latest_agent_cycle.get("status") or "unknown")
        ended = latest_agent_cycle.get("cycle_ended_at") or latest_agent_cycle.get(
            "cycle_started_at"
        )
        sources.append(
            {
                "key": "babyg",
                "label": "babyg",
                "state": "attention" if status == "failed" else "healthy",
                "detail": "latest agent cycle failed" if status == "failed" else status,
                "href": "/creator/bot",
                "action_label": "open babyg",
                "checked_at": ended,
            }
        )
    elif latest_sweep:
        sources.append(
            {
                "key": "babyg",
                "label": "babyg",
                "state": "healthy",
                "detail": str(latest_sweep.get("job_name") or "sweep"),
                "href": "/creator/bot",
                "action_label": "open babyg",
                "checked_at": latest_sweep.get("ran_at"),
            }
        )
    else:
        sources.append(
            {
                "key": "babyg",
                "label": "babyg",
                "state": "ready",
                "detail": "no confirmed background check yet",
                "href": "/creator/bot",
                "action_label": "open babyg",
                "checked_at": None,
            }
        )

    if instagram_connection:
        state = "connected"
        detail = "connected"
        checked_at = (instagram_snapshot or {}).get("captured_at")
        if checked_at:
            state = "healthy"
            detail = "account snapshot checked"
        sources.append(
            {
                "key": "instagram",
                "label": "Instagram",
                "state": state,
                "detail": detail,
                "href": "/creator/instagram/dms",
                "action_label": "open Instagram",
                "checked_at": checked_at,
            }
        )
    else:
        sources.append(
            {
                "key": "instagram",
                "label": "Instagram",
                "state": "disconnected",
                "detail": "not connected",
                "href": "/creator/instagram/connect?next=/creator",
                "action_label": "connect",
                "checked_at": None,
            }
        )

    sources.append(
        {
            "key": "gmail",
            "label": "Gmail",
            "state": "connected" if google_connection and gmail_connected else "disconnected",
            "detail": "connected" if google_connection and gmail_connected else "not connected",
            "href": "/creator/bot" if google_connection and gmail_connected else "/creator/google/connect?service=gmail&next=/creator",
            "action_label": "open babyg" if google_connection and gmail_connected else "connect",
            "checked_at": None,
        }
    )
    sources.append(
        {
            "key": "calendar",
            "label": "Calendar",
            "state": "connected" if google_connection and calendar_connected else "disconnected",
            "detail": "connected" if google_connection and calendar_connected else "not connected",
            "href": "/creator/calendar" if google_connection and calendar_connected else "/creator/google/connect?service=calendar&next=/creator",
            "action_label": "open calendar" if google_connection and calendar_connected else "connect",
            "checked_at": None,
        }
    )

    checked_at = _latest_time(
        [
            s.get("checked_at")
            for s in sources
            if s.get("state") == "healthy" and s.get("checked_at")
        ]
    )
    connected_count = sum(1 for s in sources if s.get("state") in {"connected", "healthy"})
    healthy_count = sum(1 for s in sources if s.get("state") == "healthy")
    attention_count = sum(1 for s in sources if s.get("state") == "attention")
    if checked_at:
        relative = _relative_short(checked_at)
        headline = "checked just now" if relative == "just now" else f"checked {relative} ago"
        summary = f"{healthy_count} source{'s' if healthy_count != 1 else ''} healthy"
        tone = "healthy"
    elif attention_count:
        headline = "manager status"
        summary = f"{attention_count} needs attention"
        tone = "attention"
    else:
        headline = "manager status"
        summary = f"{connected_count} connected"
        tone = "connected" if connected_count else "neutral"
    return {
        "headline": headline,
        "summary": summary,
        "tone": tone,
        "sources": sources,
    }


def _needs_you(
    *,
    manager_activity: list[dict[str, Any]],
    unread_notifs: list[dict[str, Any]],
    pending_actions: list[dict[str, Any]],
    pending_connections: list[dict[str, Any]],
    upcoming_bookings: list[dict[str, Any]],
    instagram_connection: dict[str, Any] | None,
    ig_dm_unread_count: int,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for action in pending_actions[:6]:
        preview = action.get("preview") or {}
        title = (
            preview.get("title")
            or preview.get("subject")
            or preview.get("headline")
            or _label_action(action.get("action_type"))
        )
        body = (
            preview.get("summary")
            or preview.get("body")
            or preview.get("detail")
            or _label_action(action.get("action_type"))
        )
        items.append(
            _item(
                id=f"action:{action.get('id')}",
                title=str(title or "action needs approval"),
                body=str(body or "review the staged action"),
                href=f"/creator/bot#action-{action.get('id')}",
                source=_source_from_action(action.get("action_type")),
                action_label="review",
                priority="high",
                score=360,
            )
        )

    for note in manager_activity:
        if not notifications._is_manager_activity(note):
            continue
        priority = str(note.get("priority") or "normal")
        if priority not in {"urgent", "high"}:
            continue
        items.append(_item_from_notification(note, score=_PRIORITY_SCORE[priority] + 20))

    if instagram_connection and ig_dm_unread_count > 0 and not any(
        item.get("source") == "instagram" for item in items
    ):
        items.append(
            _item(
                id="instagram:dms",
                title=f"{ig_dm_unread_count} unread Instagram DM{'' if ig_dm_unread_count == 1 else 's'}",
                body="Open the Instagram inbox when you are ready to review them.",
                href="/creator/instagram/dms",
                source="instagram",
                action_label="open",
                priority="normal",
                score=205,
            )
        )

    for booking in upcoming_bookings[:4]:
        status = str(booking.get("status") or "").lower()
        if status != "pending":
            continue
        title = str(booking.get("title") or "booking")
        items.append(
            _item(
                id=f"booking:{booking.get('id')}",
                title="confirm " + title,
                body=str(booking.get("venue_name") or "calendar item"),
                href=f"/creator/calendar/{booking.get('id')}" if booking.get("id") else "/creator/calendar",
                source="calendar",
                action_label="review",
                priority="high",
                score=320,
            )
        )

    for conn in pending_connections[:3]:
        peer = conn.get("peer") or {}
        name = (
            peer.get("full_name")
            or peer.get("instagram_handle")
            or "someone"
        )
        items.append(
            _item(
                id=f"connection:{conn.get('id')}",
                title=f"{name} wants to connect",
                body="Review the creator request.",
                href="/creator/connections",
                source="network",
                action_label="review",
                priority="normal",
                score=215,
            )
        )

    for note in unread_notifs:
        if note.get("kind") == "new_dm":
            continue
        if notifications._is_manager_activity(note):
            continue
        priority = str(note.get("priority") or "normal")
        if priority not in {"urgent", "high"}:
            continue
        items.append(_item_from_notification(note, score=_PRIORITY_SCORE[priority]))

    return _rank(items)


def _status_attention_item(status: dict[str, Any]) -> dict[str, Any]:
    failing: dict[str, Any] = next(
        (
            source
            for source in status.get("sources", [])
            if source.get("state") == "attention"
        ),
        {},
    )
    label = str(failing.get("label") or "babyg")
    detail = str(failing.get("detail") or "latest manager check needs review")
    return _item(
        id="status:manager-attention",
        title=f"{label} needs attention",
        body=detail,
        href=str(failing.get("href") or "/creator/bot"),
        source=str(failing.get("key") or "babyg"),
        action_label="review",
        priority="high",
        score=340,
        created_at=failing.get("checked_at"),
    )


def _brief(
    *,
    manager_activity: list[dict[str, Any]],
    matched_picks: list[dict[str, Any]],
    instagram_growth: dict[str, int | None],
    already_used_ids: set[str],
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for note in manager_activity:
        if not notifications._is_manager_activity(note):
            continue
        note_id = str(note.get("id") or "")
        if note_id in already_used_ids:
            continue
        items.append(_item_from_notification(note, score=_PRIORITY_SCORE.get(str(note.get("priority") or "normal"), 200)))

    growth_item = _growth_item(instagram_growth)
    if growth_item:
        items.append(growth_item)

    for card in matched_picks[:3]:
        kind = str(card.get("card_kind") or "match")
        title = str(card.get("title") or "new match")
        body = str(card.get("subtitle") or card.get("location_label") or "real Discover match")
        href = "/creator/discover"
        card_id = card.get("card_id") or card.get("id")
        if card_id:
            href = f"/creator/discover?bring_back_kind={kind}&bring_back_id={card_id}"
        items.append(
            _item(
                id=f"discover:{kind}:{card_id}",
                title=title,
                body=body,
                href=href,
                source="network",
                action_label="review",
                priority="normal",
                score=190,
            )
        )
    return _rank(items)


def _growth_item(growth: dict[str, int | None]) -> dict[str, Any] | None:
    followers = growth.get("followers_count")
    reach = growth.get("reach")
    if isinstance(followers, int) and abs(followers) >= 20:
        direction = "up" if followers > 0 else "down"
        return _item(
            id="performance:instagram:followers",
            title=f"Instagram followers are {direction}",
            body=f"{followers:+,} over the latest stored 7-day window.",
            href="/creator/performance?platform=instagram",
            source="instagram",
            action_label="see why",
            priority="normal",
            score=225,
        )
    if isinstance(reach, int) and reach >= 100:
        return _item(
            id="performance:instagram:reach",
            title="Instagram reach moved",
            body=f"{reach:+,} over the latest stored 7-day window.",
            href="/creator/performance?platform=instagram",
            source="instagram",
            action_label="see why",
            priority="normal",
            score=210,
        )
    return None


def _today(
    bookings: list[dict[str, Any]], *, calendar_connected: bool
) -> dict[str, Any]:
    now = datetime.now(UTC)
    end = now + timedelta(hours=24)
    today_events = []
    for booking in bookings or []:
        starts = _parse_time(booking.get("starts_at"))
        if starts is None or starts < now or starts > end:
            continue
        today_events.append(booking)
    return {
        "connected": calendar_connected,
        "events": today_events[:2],
        "has_later": len(today_events) > 2,
    }


def _handled(recap: dict[str, Any] | None) -> dict[str, Any] | None:
    if not recap:
        return None
    counts = recap.get("counts") or {}
    parts = []
    mapping = (
        ("proposals", "action"),
        ("nudges", "nudge"),
        ("cycles_active", "cycle"),
        ("memory_writes", "memory update"),
        ("ig_dms", "instagram dm"),
    )
    for key, label in mapping:
        n = int(counts.get(key) or 0)
        if n > 0:
            parts.append(f"{n} {label}{'s' if n != 1 else ''}")
    if not parts:
        return None
    return {
        "label": "handled recently",
        "summary": " · ".join(parts[:3]),
        "href": "/creator/bot",
        "headlines": list(recap.get("headlines") or [])[:3],
    }


def _watching(
    *,
    open_deals: int,
    opportunity_count: int,
    upcoming_count: int,
    ig_dm_unread_count: int,
) -> dict[str, Any] | None:
    parts = []
    if open_deals > 0:
        parts.append(f"{open_deals} deal{'s' if open_deals != 1 else ''}")
    if opportunity_count > 0:
        parts.append(
            f"{opportunity_count} opportunit{'ies' if opportunity_count != 1 else 'y'}"
        )
    if upcoming_count > 0:
        parts.append(f"{upcoming_count} calendar item{'s' if upcoming_count != 1 else ''}")
    if ig_dm_unread_count > 0:
        parts.append(f"{ig_dm_unread_count} Instagram DM{'s' if ig_dm_unread_count != 1 else ''}")
    if not parts:
        return None
    href = "/creator/bot"
    if open_deals > 0:
        href = "/creator/deals"
    elif opportunity_count > 0:
        href = "/creator/discover"
    elif upcoming_count > 0:
        href = "/creator/calendar"
    elif ig_dm_unread_count > 0:
        href = "/creator/instagram/dms"
    return {
        "summary": "watching " + " · ".join(parts[:4]),
        "href": href,
    }


def _item_from_notification(row: dict[str, Any], *, score: int) -> dict[str, Any]:
    metadata_raw = row.get("metadata")
    metadata: dict[str, Any] = metadata_raw if isinstance(metadata_raw, dict) else {}
    source = str(row.get("source_provider") or metadata.get("source") or "babyg").lower()
    if source not in {"instagram", "gmail", "google", "calendar", "network", "babyg"}:
        source = "babyg"
    return _item(
        id=str(row.get("id") or ""),
        title=str(row.get("title") or "manager alert"),
        body=str(row.get("body") or ""),
        href=str(row.get("link_path") or "/creator/notifications"),
        source=source,
        action_label="review",
        priority=str(row.get("priority") or "normal"),
        score=score,
        created_at=row.get("created_at"),
    )


def _item(
    *,
    id: str,
    title: str,
    body: str,
    href: str,
    source: str,
    action_label: str,
    priority: str,
    score: int,
    created_at: Any = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "body": body,
        "href": href,
        "source": source,
        "action_label": action_label,
        "priority": priority if priority in _PRIORITY_SCORE else "normal",
        "score": score,
        "created_at": created_at,
    }


def _rank(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: dict[str, dict[str, Any]] = {}
    for item in items:
        key = str(item.get("id") or item.get("href") or item.get("title"))
        existing = deduped.get(key)
        if existing is None or int(item.get("score") or 0) > int(existing.get("score") or 0):
            deduped[key] = item
    return sorted(
        deduped.values(),
        key=lambda item: (
            int(item.get("score") or 0),
            str(item.get("created_at") or ""),
        ),
        reverse=True,
    )


def _source_from_action(action_type: Any) -> str:
    text = str(action_type or "").lower()
    if "gmail" in text:
        return "gmail"
    if "calendar" in text:
        return "calendar"
    if "instagram" in text:
        return "instagram"
    return "babyg"


def _label_action(action_type: Any) -> str:
    text = str(action_type or "").replace(".", " ").replace("_", " ").strip()
    return text or "action"


def _latest_time(values: list[Any]) -> str | None:
    parsed = [(_parse_time(value), value) for value in values]
    valid = [(dt, value) for dt, value in parsed if dt is not None]
    if not valid:
        return None
    valid.sort(key=lambda pair: pair[0], reverse=True)
    return str(valid[0][1])


def _relative_short(value: Any) -> str:
    dt = _parse_time(value)
    if dt is None:
        return "recently"
    delta = datetime.now(UTC) - dt
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return "just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h"
    return f"{hours // 24}d"


def _parse_time(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
