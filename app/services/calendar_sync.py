"""Google Calendar sync into local babyg bookings."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.integrations import google_calendar
from app.services import bookings, oauth_connections

logger = logging.getLogger(__name__)


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
        events = google_calendar.list_primary_events(
            token,
            time_min=now - timedelta(days=30),
            time_max=now + timedelta(days=180),
            max_results=100,
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
        if bookings.upsert_google_event(user_id=user_id, payload=payload):
            imported += 1
        else:
            skipped += 1
    return CalendarSyncResult(imported=imported, skipped=skipped, connected=True)
