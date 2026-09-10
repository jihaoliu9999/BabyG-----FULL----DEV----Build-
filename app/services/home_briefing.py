"""Home page v5 briefing composer.

Turns raw dashboard state (already fetched in parallel by the
`/creator` route) into the five compact slots the home template
renders:

  1. `status`             — connected-integration count + per-provider rows
  2. `primary`            — the single top manager card (or None -> clear state)
  3. `next_item`          — the next upcoming booking (or None -> connect/clear)
  4. `brief`              — up to 3 non-urgent highlights
  5. `handled`+`watching` — two side-by-side compact summaries

Design rules (from the home-v5 spec):

  * Real state only. If a slot's real value is absent, return None
    and let the template collapse that block.
  * No LLM calls. This runs on every home render and needs to be
    cheap. Signals like "collab inquiry" that need Claude belong
    in the background agent loop, not here.
  * No new supabase tables. Every read here already exists in
    another service.
  * Structured plain-dict outputs so tests can assert shapes
    without importing template context.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import supabase_client
from app.services import oauth_connections

logger = logging.getLogger(__name__)


# ---- 1. status pill --------------------------------------------------

INTEGRATION_SLOTS: tuple[str, ...] = ("instagram", "gmail", "calendar")


def integration_status(
    user_id: str,
    *,
    google_connection: dict[str, Any] | None = None,
    ig_connection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Live integration state used by the babyg status pill + details.

    Callers may pass pre-fetched connection rows to avoid a second
    round-trip on the dashboard render path. When omitted, we fetch
    them here. Every field is derived from real OAuth state — a UI
    row for a provider we can't actually call is never "connected".

    Returns:
      {
        "connected_count": int,          # 0..3
        "rows": [
          {slot, label, connected: bool, needs_reconnect: bool,
           connect_href, open_href, action_label}
        ],
      }
    """
    if google_connection is None:
        try:
            google_connection = oauth_connections.get_google_connection(user_id)
        except Exception:
            logger.exception("home_briefing.integration.google_fetch_failed user=%s", user_id)
            google_connection = None
    if ig_connection is None:
        try:
            ig_connection = oauth_connections.get_instagram_connection(user_id)
        except Exception:
            logger.exception("home_briefing.integration.ig_fetch_failed user=%s", user_id)
            ig_connection = None

    ig_connected = bool(ig_connection and (ig_connection.get("access_token") or ""))
    gmail_connected = oauth_connections.google_gmail_connected(google_connection)
    calendar_connected = oauth_connections.google_calendar_connected(google_connection)

    ig_needs_reconnect = False
    if ig_connected:
        # Cheap: reuse the existing helper; don't force a Meta call here.
        try:
            ig_needs_reconnect = oauth_connections.instagram_needs_reconnect(user_id)
        except Exception:
            ig_needs_reconnect = False

    rows = [
        {
            "slot": "instagram",
            "label": "Instagram",
            "connected": ig_connected,
            "needs_reconnect": ig_needs_reconnect,
            "connect_href": "/creator/profile/settings#integrations",
            "open_href": "/creator/instagram/dms" if ig_connected else "/creator/profile/settings#integrations",
        },
        {
            "slot": "gmail",
            "label": "Gmail",
            "connected": gmail_connected,
            "needs_reconnect": False,
            "connect_href": "/creator/profile/settings#integrations",
            "open_href": "/creator/dm" if gmail_connected else "/creator/profile/settings#integrations",
        },
        {
            "slot": "calendar",
            "label": "Google Calendar",
            "connected": calendar_connected,
            "needs_reconnect": False,
            "connect_href": "/creator/profile/settings#integrations",
            "open_href": "/creator/calendar" if calendar_connected else "/creator/profile/settings#integrations",
        },
    ]
    for row in rows:
        if row["connected"] and not row["needs_reconnect"]:
            row["action_label"] = "open"
        elif row["needs_reconnect"]:
            row["action_label"] = "reconnect"
        else:
            row["action_label"] = "connect"

    return {
        "connected_count": sum(1 for r in rows if r["connected"] and not r["needs_reconnect"]),
        "rows": rows,
    }


# ---- 2. primary manager update --------------------------------------

# Category source is derived from the proposal's action_type. Keeps the
# template markup simple — one branch per source instead of per-action.
_ACTION_SOURCE_MAP: dict[str, str] = {
    "gmail.create_draft": "gmail",
    "gmail.send_email": "gmail",
    "gmail.send_draft": "gmail",
    "calendar.create_event": "calendar",
    "calendar.update_event": "calendar",
    "calendar.delete_event": "calendar",
    "instagram.send_dm": "instagram",
    "create_gmail_draft": "gmail",
}

_ACTION_CATEGORY_LABEL: dict[str, str] = {
    "gmail.create_draft": "draft email",
    "gmail.send_email": "send email",
    "gmail.send_draft": "send draft",
    "calendar.create_event": "calendar event",
    "calendar.update_event": "calendar change",
    "calendar.delete_event": "calendar change",
    "instagram.send_dm": "instagram reply",
    "create_gmail_draft": "draft email",
    "create_booking": "booking",
    "create_content_reminder": "reminder",
    "submit_creator_listing": "listing",
}


def primary_manager_update(
    *,
    pending_actions: list[dict[str, Any]] | None,
    unread_notifs: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Pick the single most-actionable card for the top of home.

    Priority order:
      1. Oldest pending `action_proposals` row (something babyg is
         waiting on the creator to tap through).
      2. Newest unread notification with a `link_path` (e.g. brand
         inquiry, deal follow-up).

    Returns None when nothing qualifies -> template renders the
    compact clear state instead.
    """
    if pending_actions:
        proposal = pending_actions[0]
        action_type = str(proposal.get("action_type") or "")
        preview = proposal.get("preview") or {}
        title = (
            preview.get("title")
            or preview.get("subject")
            or preview.get("headline")
            or _ACTION_CATEGORY_LABEL.get(action_type, action_type.replace(".", " · "))
        )
        body = (
            preview.get("body")
            or preview.get("summary")
            or preview.get("detail")
            or ""
        )
        return {
            "source": _ACTION_SOURCE_MAP.get(action_type, "babyg"),
            "category": _ACTION_CATEGORY_LABEL.get(action_type, "action"),
            "title": str(title)[:120],
            "body": str(body)[:200],
            "created_at": proposal.get("created_at"),
            "primary_href": f"/creator/bot#action-{proposal.get('id')}",
            "primary_label": "review",
            "secondary_href": f"/creator/bot#action-{proposal.get('id')}",
            "secondary_label": "view details",
        }

    if unread_notifs:
        n = unread_notifs[0]
        source = _source_from_notification_kind(str(n.get("kind") or ""))
        return {
            "source": source,
            "category": str(n.get("kind") or "notice").replace("_", " "),
            "title": str(n.get("title") or "")[:120] or "there's an update",
            "body": str(n.get("body") or "")[:200],
            "created_at": n.get("created_at"),
            "primary_href": str(n.get("link_path") or "/creator/notifications"),
            "primary_label": "review",
            "secondary_href": "/creator/notifications",
            "secondary_label": "view details",
        }

    return None


def _source_from_notification_kind(kind: str) -> str:
    if kind == "new_dm":
        return "instagram"
    if kind in {"booking_reminder"}:
        return "calendar"
    if kind in {"job_match", "connection_request", "collab_match"}:
        return "babyg"
    return "babyg"


# ---- 3. brief --------------------------------------------------------

BRIEF_MAX = 3


def brief_rows(
    *,
    matched_picks: list[dict[str, Any]] | None,
    ig_dm_unread_count: int,
    overnight_recap: dict[str, Any] | None,
    performance_view: Any | None,
) -> list[dict[str, Any]]:
    """Up to 3 non-urgent highlights.

    Every row includes a slot label so the template can pick a
    recognizable icon (bar-chart, instagram, magnifier, calendar,
    lightbulb). Never fabricates data — a source that has nothing
    real to say returns no row.

    Order matters: highest signal first, since the template truncates
    to BRIEF_MAX.
    """
    rows: list[dict[str, Any]] = []

    # 1. Performance signal (real IG account snapshot from stats_merge).
    perf_row = _brief_performance_row(performance_view)
    if perf_row:
        rows.append(perf_row)

    # 2. Instagram DM (only when creator has unread IG DMs the agent
    #    hasn't already escalated to primary).
    if int(ig_dm_unread_count or 0) > 0:
        n = int(ig_dm_unread_count)
        rows.append({
            "slot": "instagram",
            "title": f"{n} unread instagram dm{'s' if n != 1 else ''}",
            "detail": "open the ig inbox to reply",
            "href": "/creator/instagram/dms",
        })

    # 3. Opportunity from discover (first matched pick when there is one).
    opp = _first_opportunity_pick(matched_picks)
    if opp:
        rows.append(opp)

    # 4. Overnight recap headlines as fallback lines (memory rewrite,
    #    thinking cycles) — only if we still have room and the recap has
    #    something meaty enough that a headline reads standalone.
    if overnight_recap and overnight_recap.get("headlines"):
        for headline in overnight_recap["headlines"]:
            if len(rows) >= BRIEF_MAX:
                break
            if _headline_already_covered(headline, rows):
                continue
            rows.append({
                "slot": "recap",
                "title": str(headline)[:100],
                "detail": "since your last check-in",
                "href": "/creator/bot",
            })

    return rows[:BRIEF_MAX]


def _brief_performance_row(view: Any) -> dict[str, Any] | None:
    if not view:
        return None
    top_rows = getattr(view, "rows", None) or []
    if not top_rows:
        return None
    # First row from performance_view has the highest signal for this
    # account. We only surface reach/engagement when we have a real
    # number for it — no invented percentages.
    top = top_rows[0]
    reach = None
    for key in ("reach", "impressions", "engagement", "like_count"):
        value = _try_int(getattr(top, key, None) if not isinstance(top, dict) else top.get(key))
        if value is not None:
            reach = (key, value)
            break
    if reach is None:
        return None
    key, value = reach
    return {
        "slot": "performance",
        "title": f"top post — {_format_int(value)} {key.replace('_', ' ')}",
        "detail": "open performance for the full breakdown",
        "href": "/creator/performance",
    }


def _first_opportunity_pick(
    matched_picks: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not matched_picks:
        return None
    for pick in matched_picks:
        if pick.get("card_kind") != "opportunity":
            continue
        title = str(pick.get("title") or "").strip()
        if not title:
            continue
        detail = str(pick.get("subtitle") or "").strip()
        if not detail and pick.get("deadline"):
            detail = "opportunity in discover"
        return {
            "slot": "opportunity",
            "title": title[:100],
            "detail": detail[:100] or "worth a look",
            "href": f"/creator/discover?bring_back_kind=opportunity&bring_back_id={pick.get('card_id')}",
        }
    return None


def _headline_already_covered(
    headline: str, existing_rows: list[dict[str, Any]]
) -> bool:
    """Skip a recap headline if the same source already produced a
    row (e.g. we already showed the IG DM count above, don't repeat)."""
    h = headline.lower()
    for row in existing_rows:
        slot = row.get("slot") or ""
        if slot == "instagram" and "instagram dm" in h:
            return True
        if slot == "performance" and "post" in h:
            return True
    return False


# ---- 4. handled + watching -------------------------------------------


def handled_today(user_id: str, *, now: datetime | None = None) -> int:
    """Number of action_proposals moved to a terminal 'done' state
    since midnight UTC today. Terminal states: 'executed', 'cancelled'.

    Never raises — a supabase blip returns 0 so the card degrades to a
    truthful "no activity yet today" rendering instead of blowing up.
    """
    now = now or datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    try:
        result = (
            supabase_client.get_service_client()
            .table("action_proposals")
            .select("id")
            .eq("user_id", user_id)
            .in_("status", ["executed", "cancelled"])
            .gte("updated_at", day_start.isoformat())
            .limit(200)
            .execute()
        )
    except Exception:
        logger.exception("home_briefing.handled_today.read_failed user=%s", user_id)
        return 0
    rows = list(getattr(result, "data", None) or [])
    return len(rows)


def watching_summary(
    *,
    matched_picks: list[dict[str, Any]] | None,
    pending_actions_all: list[dict[str, Any]] | None,
) -> dict[str, int]:
    """Counts babyg is "watching" — pending action proposals + surfaced
    discover opportunities. Both are real state, not invented totals.
    """
    deals = len(pending_actions_all or [])
    opportunities = sum(
        1 for m in (matched_picks or []) if (m or {}).get("card_kind") == "opportunity"
    )
    return {"deals": deals, "opportunities": opportunities}


# ---- shared helpers --------------------------------------------------


def relative_ago(created_at: str | None, *, now: datetime | None = None) -> str:
    """"2h ago" / "3d ago" — cheap best-effort formatter for the primary
    card's timestamp. Returns empty string when we can't parse."""
    if not created_at:
        return ""
    try:
        ts = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except Exception:
        return ""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = (now or datetime.now(UTC)) - ts
    if delta < timedelta(minutes=1):
        return "just now"
    if delta < timedelta(hours=1):
        return f"{int(delta.total_seconds() // 60)}m ago"
    if delta < timedelta(days=1):
        return f"{int(delta.total_seconds() // 3600)}h ago"
    return f"{delta.days}d ago"


def _try_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _format_int(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}m".replace(".0m", "m")
    if n >= 1_000:
        return f"{n / 1_000:.1f}k".replace(".0k", "k")
    return str(n)
