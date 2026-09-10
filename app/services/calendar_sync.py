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
