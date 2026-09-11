"""Google Calendar sync into local babyg bookings."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.integrations import google_calendar
from app.services import bookings, oauth_connections

logger = logging.getLogger(__name__)

# Throttle for auto-sync freshness: opening the calendar page
# repeatedly must not stampede Google. Manual sync (the button)
# bypasses this.
AUTO_SYNC_MIN_INTERVAL_SECONDS = 120.0

# Timezone cache: primary calendar timezone lookups are cheap but
# still hit the network. Cache per-user for the same throttle window
# as auto-sync so a Home render + Calendar render inside the same
# window don't fire two lookups.
TIMEZONE_CACHE_TTL_SECONDS = 900.0

# Process-local last-sync timestamps. A multi-process deploy will
# duplicate at worst one Google call per box per interval, which
# is acceptable.
_LAST_AUTO_SYNC_AT: dict[str, float] = {}

# Process-local timezone cache: user_id -> (expires_at_monotonic, tz_name or None).
# Sync also refreshes this when it fetches the primary calendar timezone.
_TIMEZONE_CACHE: dict[str, tuple[float, str | None]] = {}


@dataclass(frozen=True)
class CalendarSyncResult:
    imported: int = 0
    skipped: int = 0
    connected: bool = False
    error: str | None = None


def sync_google_calendar(user_id: str) -> CalendarSyncResult:
    """Sync Google Calendar into local `bookings`.

    Emits safe INFO logs at every stage so we can diagnose in
    production without needing to reproduce locally:
      * calendar_sync.start                       — sync began
      * calendar_sync.calendars                   — how many + hint ids
      * calendar_sync.events_per_calendar         — per-calendar counts
      * calendar_sync.persistence                 — imported/skipped/cancelled
      * calendar_sync.complete OR .error          — outcome + reason
    No tokens, secrets, or event bodies are logged.
    """
    logger.info("calendar_sync.start user=%s", user_id)
    token = oauth_connections.access_token_for_google(user_id)
    if not token:
        logger.info("calendar_sync.error user=%s reason=not_connected", user_id)
        return CalendarSyncResult(error="not_connected")
    # Prime the primary-calendar timezone before pulling events so the
    # payload builder can anchor all-day dates in the correct zone. A
    # failure returns None; downstream still runs, all-day events just
    # fall back to UTC anchoring as before.
    primary_tz = google_calendar.get_primary_calendar_timezone(token)
    _remember_timezone(user_id, primary_tz)
    logger.info(
        "calendar_sync.timezone user=%s has_tz=%s",
        user_id,
        "yes" if primary_tz else "no",
    )
    now = datetime.now(UTC)
    time_min = now - timedelta(days=30)
    time_max = now + timedelta(days=180)
    try:
        calendar_ids = google_calendar.list_calendar_ids(token)
        logger.info(
            "calendar_sync.calendars user=%s count=%s hints=%s",
            user_id,
            len(calendar_ids),
            [_id_hint(cid) for cid in calendar_ids[:10]],
        )
        events: list[dict] = []
        for calendar_id in calendar_ids:
            calendar_events = google_calendar.list_events(
                token,
                calendar_id=calendar_id,
                time_min=time_min,
                time_max=time_max,
                max_results=250,
            )
            logger.info(
                "calendar_sync.events_per_calendar user=%s calendar_hint=%s count=%s",
                user_id,
                _id_hint(calendar_id),
                len(calendar_events),
            )
            events.extend(calendar_events)
    except google_calendar.GoogleCalendarError as exc:
        logger.info(
            "calendar_sync.error user=%s reason=google_error detail=%s",
            user_id,
            str(exc)[:200],
        )
        return CalendarSyncResult(connected=True, error="google_error")

    imported = 0
    skipped = 0
    cancelled = 0
    for event in events:
        payload = google_calendar.event_to_booking_payload(
            event, default_timezone=primary_tz
        )
        if not payload:
            skipped += 1
            continue
        if payload.get("status") == "cancelled":
            if bookings.cancel_google_event(
                user_id=user_id,
                google_calendar_id=str(payload.get("google_calendar_id") or "primary"),
                google_event_id=str(payload.get("google_event_id") or ""),
            ):
                cancelled += 1
            else:
                skipped += 1
        elif bookings.upsert_google_event(user_id=user_id, payload=payload):
            imported += 1
        else:
            skipped += 1
    logger.info(
        "calendar_sync.complete user=%s total_events=%s imported=%s skipped=%s cancelled=%s",
        user_id,
        len(events),
        imported,
        skipped,
        cancelled,
    )
    return CalendarSyncResult(
        imported=imported + cancelled,
        skipped=skipped,
        connected=True,
    )


def _id_hint(value: str) -> str:
    """Safe, non-secret hint for logging opaque IDs."""
    raw = str(value or "")
    if not raw:
        return "<empty>"
    if len(raw) <= 12:
        return raw
    return raw[:6] + "..." + raw[-6:]


def _remember_timezone(user_id: str, tz_name: str | None) -> None:
    """Cache a per-user primary-calendar timezone lookup."""
    if not user_id:
        return
    _TIMEZONE_CACHE[user_id] = (
        time.monotonic() + TIMEZONE_CACHE_TTL_SECONDS,
        tz_name or None,
    )


def effective_timezone(user_id: str) -> str | None:
    """Return the user's effective calendar IANA timezone, or None.

    Priority per product spec:
      1. Google Calendar primary calendar timezone (fetched on demand,
         cached in-process for TIMEZONE_CACHE_TTL_SECONDS).
      2. None — caller falls back to UTC. babyg does not currently
         persist a per-user timezone anywhere else, so there is no
         second source to consult; when Google is not connected the
         calendar is empty anyway, and UTC is the safe fallback.

    Never raises. Never logs the timezone value. A missing token, a
    disconnected connection, or an HTTP failure all resolve to None.
    """
    if not user_id:
        return None
    entry = _TIMEZONE_CACHE.get(user_id)
    now_mono = time.monotonic()
    if entry is not None and entry[0] > now_mono:
        return entry[1]
    try:
        token = oauth_connections.access_token_for_google(user_id)
    except Exception:
        logger.exception("calendar_sync.effective_timezone token lookup failed")
        _remember_timezone(user_id, None)
        return None
    if not token:
        _remember_timezone(user_id, None)
        return None
    tz_name: str | None = None
    try:
        tz_name = google_calendar.get_primary_calendar_timezone(token)
    except Exception:
        logger.exception("calendar_sync.effective_timezone lookup crashed")
        tz_name = None
    _remember_timezone(user_id, tz_name)
    return tz_name


def today_in_zone(tz_name: str | None) -> date:
    """Today's calendar date resolved in ``tz_name``, UTC fallback.

    Used everywhere we need the current calendar day (today highlight,
    week boundaries, month bounds). Never raises. An unknown timezone
    name falls back to UTC rather than the server's local clock — the
    server timezone must never determine the visible day.
    """
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name)).date()
        except (ZoneInfoNotFoundError, ValueError):
            pass
    return datetime.now(UTC).date()


def maybe_auto_sync(user_id: str) -> CalendarSyncResult | None:
    """Trigger a Google sync at most every AUTO_SYNC_MIN_INTERVAL_SECONDS
    per user. Returns the sync result when the sync actually fires,
    None when throttled or the user id is missing.

    Never raises — callers use it as best-effort freshness on the
    calendar render path so newly-added real Google events surface
    without the creator having to tap the manual sync button.
    """
    if not user_id:
        return None
    now_mono = time.monotonic()
    last = _LAST_AUTO_SYNC_AT.get(user_id)
    if last is not None and (now_mono - last) < AUTO_SYNC_MIN_INTERVAL_SECONDS:
        return None
    _LAST_AUTO_SYNC_AT[user_id] = now_mono
    try:
        return sync_google_calendar(user_id)
    except Exception:
        logger.exception("Google Calendar auto-sync crashed for user %s", user_id)
        return None
