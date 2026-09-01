"""Instagram Phase C — daily snapshot + growth history.

The performance page and the AI tool already read the current account
totals from Meta via `stats_merge.instagram_account_snapshot`. That's
one point in time. To answer "how did your follower count / reach
change this week?" babyg needs a series, not a point.

This module owns that series:

  snapshot_daily(user_id)         upsert today's row from Meta
  latest_snapshot(user_id)        most recent stored row
  growth_over(user_id, days)      delta of totals + insights over N days
  history(user_id, days=30)       raw rows, newest first

Every helper is best-effort. Supabase failures never raise; they log
status-only and return an empty result. Nothing here initiates an
external Graph call except `snapshot_daily`, which delegates to
`stats_merge.instagram_account_snapshot` (which already respects
`is_configured`, connection presence, and the token refresh cache).

The daily upsert is idempotent — the (user_id, captured_on) unique
index is what makes a re-run a no-op instead of a duplicate. That's
what lets bot_jobs re-play a failed slot safely.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.integrations import instagram_meta
from app.services import stats_merge

logger = logging.getLogger(__name__)

_TABLE = "instagram_metrics_daily"

# Fields we store and can compute growth on. Kept in one tuple so a
# schema change updates every read-side path consistently.
METRIC_FIELDS: tuple[str, ...] = (
    "followers_count",
    "follows_count",
    "media_count",
    "reach",
    "impressions",
    "profile_views",
)


def snapshot_daily(user_id: str) -> dict[str, Any] | None:
    """Pull today's Meta snapshot for the creator and upsert one row.

    Returns the stored row, or None if the snapshot failed or the creator
    has no Instagram connection. Called by the daily bot_jobs sweep;
    idempotent per (user_id, day) so a re-run of the same slot no-ops
    on the DB side.
    """
    uid = safe_uuid(user_id)
    if not uid:
        return None
    snapshot, status = stats_merge.instagram_account_snapshot(uid)
    # No connection or config → nothing to store. Not an error.
    if status != stats_merge.IG_STATUS_OK:
        return None
    # If every field is None the snapshot didn't yield anything useful;
    # skip storage rather than write an empty row.
    if all(snapshot.get(f) is None for f in METRIC_FIELDS):
        return None

    payload = {
        "user_id": uid,
        "captured_on": _today_utc().isoformat(),
        "captured_at": datetime.now(UTC).isoformat(),
        **{f: snapshot.get(f) for f in METRIC_FIELDS},
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table(_TABLE)
            .upsert(payload, on_conflict="user_id,captured_on")
            .execute()
        )
    except Exception:
        logger.exception("instagram_metrics_daily upsert failed: %s", uid)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else payload


def latest_snapshot(user_id: str) -> dict[str, Any] | None:
    """Most recent stored row for this creator, or None."""
    uid = safe_uuid(user_id)
    if not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table(_TABLE)
            .select("*")
            .eq("user_id", uid)
            .order("captured_on", desc=True)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("instagram_metrics_daily latest read failed: %s", uid)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def history(user_id: str, *, days: int = 30) -> list[dict[str, Any]]:
    """Return the last `days` daily snapshots for this creator.

    Newest first. Missing days are simply absent (not padded). Caller
    can drop-fill on the render side if a chart wants continuity.
    """
    uid = safe_uuid(user_id)
    if not uid:
        return []
    bounded = max(1, min(int(days or 30), 365))
    try:
        result = (
            supabase_client.get_service_client()
            .table(_TABLE)
            .select("*")
            .eq("user_id", uid)
            .order("captured_on", desc=True)
            .limit(bounded)
            .execute()
        )
    except Exception:
        logger.exception("instagram_metrics_daily history read failed: %s", uid)
        return []
    return list(getattr(result, "data", None) or [])


def growth_over(
    user_id: str, *, days: int = 7
) -> dict[str, int | None]:
    """Return delta per metric across the window.

    Growth is `latest - earliest_within_window`. If either endpoint is
    None for a given metric, that metric's delta is None (not zero) so
    the caller can distinguish "no change" from "no data".

    `days=7` means "compare today's snapshot to the oldest one taken
    within the past 7 days." If the creator only has one snapshot on
    file, every delta is None — a manager doesn't fabricate growth
    from a single data point.
    """
    uid = safe_uuid(user_id)
    if not uid:
        return dict.fromkeys(METRIC_FIELDS)
    bounded_days = max(1, min(int(days or 7), 365))
    cutoff = (_today_utc() - timedelta(days=bounded_days)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table(_TABLE)
            .select("*")
            .eq("user_id", uid)
            .gte("captured_on", cutoff)
            .order("captured_on", desc=True)
            .execute()
        )
    except Exception:
        logger.exception("instagram_metrics_daily growth read failed: %s", uid)
        return dict.fromkeys(METRIC_FIELDS)

    rows = list(getattr(result, "data", None) or [])
    if len(rows) < 2:
        # Not enough history to compute a delta.
        return dict.fromkeys(METRIC_FIELDS)

    newest = rows[0]
    oldest = rows[-1]
    deltas: dict[str, int | None] = {}
    for field in METRIC_FIELDS:
        new_val = _int_or_none(newest.get(field))
        old_val = _int_or_none(oldest.get(field))
        if new_val is None or old_val is None:
            deltas[field] = None
        else:
            deltas[field] = new_val - old_val
    return deltas


def verified_follower_count(user_id: str) -> int | None:
    """Follower count from the latest stored snapshot, or None.

    Phase D convenience: the profile view + creator card use this to
    show a verified follower number when the creator has connected
    Instagram, falling back to the self-reported `follower_range`
    otherwise. Never lies — no snapshot = None, so the template
    always falls back cleanly.
    """
    row = latest_snapshot(user_id)
    if row is None:
        return None
    return _int_or_none(row.get("followers_count"))


def _today_utc() -> date:
    return datetime.now(UTC).date()


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# Re-export the module the snapshot ultimately touches so a test can
# monkeypatch it without an extra import in the test file.
__all__ = [
    "METRIC_FIELDS",
    "snapshot_daily",
    "latest_snapshot",
    "verified_follower_count",
    "history",
    "growth_over",
    "instagram_meta",
]
