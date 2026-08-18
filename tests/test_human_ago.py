"""human_ago Jinja filter — compact relative timestamp labels used on
DM list rows and elsewhere. Absent + malformed values must degrade to
harmless strings, never raise."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.core.templating import _human_ago


def _iso(delta: timedelta) -> str:
    return (datetime.now(UTC) - delta).isoformat()


def test_empty_and_none_render_empty_string() -> None:
    assert _human_ago(None) == ""
    assert _human_ago("") == ""
    assert _human_ago("   ") == ""


def test_under_a_minute_reads_as_now() -> None:
    assert _human_ago(_iso(timedelta(seconds=10))) == "now"


def test_minutes_bucket() -> None:
    assert _human_ago(_iso(timedelta(minutes=12))) == "12m"


def test_hours_bucket() -> None:
    assert _human_ago(_iso(timedelta(hours=3))) == "3h"


def test_days_bucket_within_a_week() -> None:
    assert _human_ago(_iso(timedelta(days=2))) == "2d"


def test_older_than_a_week_uses_month_day() -> None:
    stamp = _iso(timedelta(days=45))
    out = _human_ago(stamp)
    # "mmm dd" for same-year OR "mmm dd, yyyy" for older; both start with
    # a lowercase 3-letter month.
    assert len(out.split()) >= 2
    assert out[:3].isalpha() and out[:3].islower()


def test_malformed_falls_back_to_prefix() -> None:
    """Bad iso string returns the first 10 chars — enough to see what
    was there without exploding the page."""
    assert _human_ago("not-an-iso") == "not-an-iso"[:10]


def test_naive_iso_is_treated_as_utc() -> None:
    """Some rows come without tzinfo; must not crash."""
    # A timestamp 2h ago without timezone
    naive = (datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2)).isoformat()
    out = _human_ago(naive)
    assert out.endswith("h")
