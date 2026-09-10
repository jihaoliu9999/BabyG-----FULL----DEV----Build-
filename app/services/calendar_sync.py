"""Google Calendar sync into local babyg bookings."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.integrations import google_calendar
from app.services import bookings, oauth_connections

logger = logging.getLogger(__name__)

# Throttle for auto-sync freshness: opening the calendar page
# repeatedly must not stampede Google. Manual sync (the button)
# bypasses this.
AUTO_SYNC_MIN_INTERVAL_SECONDS = 120.0

# Process-local last-sync timestamps. A multi-process deploy will
# duplicate at worst one Google call per box per interval, which
# is acceptable.
_LAST_AUTO_SYNC_AT: dict[str, float] = {}


@dataclass(frozen=True)
class CalendarSyncResult:
    imported: int = 0
    skipped: int = 0
    connected: bool = False
    error: str | None = None


def sync_google_calendar(user_id: str) -> CalendarSyncResult:
    token = oauth_connections.access_token_for_google(user_id)
    if not token:
        return CalendarSyncResult(error="not_connected")
    now = datetime.now(UTC)
    try:
        calendar_ids = google_calendar.list_calendar_ids(token)
        events: list[dict] = []
        for calendar_id in calendar_ids:
            events.extend(
                google_calendar.list_events(
                    token,
                    calendar_id=calendar_id,
                    time_min=now - timedelta(days=30),
                    time_max=now + timedelta(days=180),
                    max_results=250,
                )
            )
    except google_calendar.GoogleCalendarError:
        logger.info("Google Calendar sync failed for user %s", user_id)
        return CalendarSyncResult(connected=True, error="google_error")

    imported = 0
    skipped = 0
    for event in events:
        payload = google_calendar.event_to_booking_payload(event)
        if not payload:
            skipped += 1
            continue
        if payload.get("status") == "cancelled":
            if bookings.cancel_google_event(
                user_id=user_id,
                google_calendar_id=str(payload.get("google_calendar_id") or "primary"),
                google_event_id=str(payload.get("google_event_id") or ""),
            ):
                imported += 1
            else:
                skipped += 1
        elif bookings.upsert_google_event(user_id=user_id, payload=payload):
            imported += 1
        else:
            skipped += 1
    return CalendarSyncResult(imported=imported, skipped=skipped, connected=True)


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
