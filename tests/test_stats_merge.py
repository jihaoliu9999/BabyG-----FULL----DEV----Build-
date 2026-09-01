"""Performance-page merge layer (Phase 4 step 1).

Service-level tests. Mocks the two sources at their module boundary —
`performance.list_for_user` and the `instagram_meta` + `oauth_connections`
helpers. No DB, no HTTP.
"""

from __future__ import annotations

import pytest

from app.integrations import instagram_meta
from app.services import oauth_connections, performance, stats_merge


@pytest.fixture(autouse=True)
def _quiet_logger(caplog):
    """Defensive try/except blocks log on unexpected failures. The
    tests intentionally raise to exercise those paths, so silence the
    expected ERROR lines."""
    caplog.set_level("CRITICAL")
    yield


def _make_media(
    *,
    media_id: str,
    caption: str | None = None,
    media_type: str | None = "IMAGE",
    permalink: str | None = None,
    timestamp: str | None = None,
    like_count: int | None = None,
    comments_count: int | None = None,
) -> instagram_meta.InstagramMedia:
    return instagram_meta.InstagramMedia(
        media_id=media_id,
        caption=caption,
        media_type=media_type,
        permalink=permalink,
        timestamp=timestamp,
        like_count=like_count,
        comments_count=comments_count,
    )


# ---------------------------------------------------------------------------
# manual-only — the default path when IG isn't connected
# ---------------------------------------------------------------------------


def test_manual_only_when_instagram_not_configured(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    # If IG is unconfigured the connection lookup MUST NOT run.
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: pytest.fail("must not hit IG storage when unconfigured"),
    )
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [
            {
                "week_start_date": "2026-06-01",
                "engagement_rate": 4.2,
                "follower_delta": 120,
                "active_brand_deals_count": 1,
                "active_brand_deals_value": 800,
                "notes": "good week",
            }
        ],
    )

    rows = stats_merge.combined_performance("creator-1")
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "manual"
    assert row.title == "period starting 2026-06-01"
    assert row.timestamp == "2026-06-01"
    assert row.permalink is None
    assert row.metrics == {
        "engagement_rate": 4.2,
        "follower_delta": 120,
        "deal_count": 1,
        "deal_value": 800,
    }
    assert row.notes == "good week"


def test_manual_only_when_instagram_not_connected(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections, "get_instagram_connection", lambda _u: None
    )
    # No token lookup should happen if no connection row exists.
    monkeypatch.setattr(
        oauth_connections,
        "access_token_for_instagram",
        lambda _u: pytest.fail("must not run when connection missing"),
    )
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [
            {"week_start_date": "2026-06-01", "engagement_rate": 3.1}
        ],
    )

    rows = stats_merge.combined_performance("creator-1")
    assert [r.source for r in rows] == ["manual"]


def test_empty_when_both_sources_empty(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])
    rows = stats_merge.combined_performance("creator-1")
    assert rows == []


# ---------------------------------------------------------------------------
# instagram path
# ---------------------------------------------------------------------------


def _patch_connected(monkeypatch, *, ig_user_id: str = "ig-123"):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider": "instagram", "provider_account_id": ig_user_id},
    )
    monkeypatch.setattr(
        oauth_connections, "instagram_account_id", lambda _c: ig_user_id
    )
    monkeypatch.setattr(
        oauth_connections, "access_token_for_instagram", lambda _u: "long-token"
    )


def test_instagram_rows_are_source_tagged_and_carry_real_metrics(monkeypatch):
    _patch_connected(monkeypatch)
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])
    monkeypatch.setattr(
        instagram_meta,
        "get_user_media",
        lambda token, *, ig_user_id, limit: [
            _make_media(
                media_id="m1",
                caption="dinner at boia\nsecond line",
                media_type="IMAGE",
                permalink="https://www.instagram.com/p/m1",
                timestamp="2026-06-08T18:00:00+0000",
                like_count=120,
                comments_count=4,
            ),
        ],
    )
    monkeypatch.setattr(
        instagram_meta,
        "get_media_insights",
        lambda token, *, media_id: {
            "engagement": 50,
            "reach": 900,
            "impressions": None,
            "saved": 6,
        },
    )

    rows = stats_merge.combined_performance("creator-1", ig_limit=3)
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "instagram"
    # Title is the first line of the caption (clamped at 80 chars).
    assert row.title == "dinner at boia"
    assert row.permalink == "https://www.instagram.com/p/m1"
    # Missing impressions stays out — never invented.
    assert row.metrics == {
        "likes": 120,
        "comments": 4,
        "reach": 900,
        "engagement": 50,
        "saved": 6,
    }


def test_instagram_failure_falls_back_to_manual_only(monkeypatch):
    _patch_connected(monkeypatch)
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [{"week_start_date": "2026-06-01", "engagement_rate": 5.0}],
    )

    def _boom(token, **kw):
        raise instagram_meta.InstagramError("graph 503")

    monkeypatch.setattr(instagram_meta, "get_user_media", _boom)
    rows = stats_merge.combined_performance("creator-1")
    # Manual row survives; no IG rows; no exception bubbled.
    assert [r.source for r in rows] == ["manual"]


def test_instagram_unexpected_exception_also_falls_back(monkeypatch):
    """A non-Graph error (e.g. supabase storage hiccup during
    access_token_for_instagram) must still degrade gracefully —
    not crash the stats page."""
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider": "instagram", "provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        oauth_connections, "instagram_account_id", lambda _c: "ig-1"
    )

    def _token_boom(_u):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(
        oauth_connections, "access_token_for_instagram", _token_boom
    )
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [{"week_start_date": "2026-06-01"}],
    )

    rows = stats_merge.combined_performance("creator-1")
    assert [r.source for r in rows] == ["manual"]


def test_per_post_insights_failure_keeps_media_row(monkeypatch):
    """One bad insights call shouldn't drop the post. Like / comment
    counts from the media row are still real."""
    _patch_connected(monkeypatch)
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])
    monkeypatch.setattr(
        instagram_meta,
        "get_user_media",
        lambda token, *, ig_user_id, limit: [
            _make_media(
                media_id="m1",
                caption="solo",
                permalink="https://www.instagram.com/p/m1",
                timestamp="2026-06-08T18:00:00+0000",
                like_count=10,
                comments_count=1,
            ),
        ],
    )

    def _insights_boom(token, *, media_id):
        raise instagram_meta.InstagramError("insights blew up")

    monkeypatch.setattr(
        instagram_meta, "get_media_insights", _insights_boom
    )

    rows = stats_merge.combined_performance("creator-1")
    assert len(rows) == 1
    assert rows[0].source == "instagram"
    # Like + comments still surface; insight metrics are absent.
    assert rows[0].metrics == {"likes": 10, "comments": 1}


# ---------------------------------------------------------------------------
# merging + ordering
# ---------------------------------------------------------------------------


def test_manual_and_instagram_intermix_by_timestamp(monkeypatch):
    _patch_connected(monkeypatch)
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [
            {"week_start_date": "2026-06-01", "engagement_rate": 4.0},
            {"week_start_date": "2026-05-25", "engagement_rate": 3.5},
        ],
    )
    monkeypatch.setattr(
        instagram_meta,
        "get_user_media",
        lambda token, *, ig_user_id, limit: [
            _make_media(
                media_id="m_recent",
                timestamp="2026-06-08T12:00:00+0000",
                like_count=50,
                comments_count=2,
            ),
            _make_media(
                media_id="m_old",
                timestamp="2026-05-20T12:00:00+0000",
                like_count=30,
                comments_count=1,
            ),
        ],
    )
    monkeypatch.setattr(
        instagram_meta, "get_media_insights", lambda *_a, **_kw: {}
    )

    rows = stats_merge.combined_performance("creator-1", ig_limit=5)
    # Expect: m_recent (2026-06-08) > week 2026-06-01 > week 2026-05-25 > m_old (2026-05-20)
    timeline = [(r.source, r.timestamp) for r in rows]
    assert timeline == [
        ("instagram", "2026-06-08T12:00:00+0000"),
        ("manual", "2026-06-01"),
        ("manual", "2026-05-25"),
        ("instagram", "2026-05-20T12:00:00+0000"),
    ]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def test_has_instagram_data_true_when_connected(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider": "instagram"},
    )
    assert stats_merge.has_instagram_data("creator-1") is True


def test_has_instagram_data_false_when_unconfigured(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    assert stats_merge.has_instagram_data("creator-1") is False


def test_has_instagram_data_false_on_exception(monkeypatch):
    """Treat oauth lookup hiccups as "not connected" rather than
    crashing the page. The page-foot copy stays at the safe default."""
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)

    def _boom(_u):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(oauth_connections, "get_instagram_connection", _boom)
    assert stats_merge.has_instagram_data("creator-1") is False


def test_ig_limit_is_clamped(monkeypatch):
    """Page loads must not let a runaway client pull arbitrary numbers
    of posts. The clamp caps at HARD_IG_LIMIT regardless of input."""
    _patch_connected(monkeypatch)
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])

    seen: dict = {}

    def _capture(token, *, ig_user_id, limit):
        seen["limit"] = limit
        return []

    monkeypatch.setattr(instagram_meta, "get_user_media", _capture)
    stats_merge.combined_performance("creator-1", ig_limit=999)
    assert seen["limit"] == stats_merge.HARD_IG_LIMIT


# ---------------------------------------------------------------------------
# Slice 4: performance_view + instagram_status
# Distinguish legitimately-empty from upstream-failure so the page
# can show a clear "temporarily unavailable" banner.
# ---------------------------------------------------------------------------


def test_performance_view_status_not_configured(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])
    view = stats_merge.performance_view("creator-1")
    assert view.rows == []
    assert view.instagram_status == stats_merge.IG_STATUS_NOT_CONFIGURED


def test_performance_view_status_not_connected(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections, "get_instagram_connection", lambda _u: None
    )
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [{"week_start_date": "2026-06-01"}],
    )
    view = stats_merge.performance_view("creator-1")
    assert [r.source for r in view.rows] == ["manual"]
    assert view.instagram_status == stats_merge.IG_STATUS_NOT_CONNECTED


def test_performance_view_status_ok_when_connected_with_data(monkeypatch):
    _patch_connected(monkeypatch)
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])
    monkeypatch.setattr(
        instagram_meta,
        "get_user_media",
        lambda token, *, ig_user_id, limit: [
            _make_media(
                media_id="m1",
                timestamp="2026-06-08T18:00:00+0000",
                like_count=10,
                comments_count=1,
            ),
        ],
    )
    monkeypatch.setattr(
        instagram_meta, "get_media_insights", lambda *_a, **_kw: {}
    )
    view = stats_merge.performance_view("creator-1")
    assert any(r.source == "instagram" for r in view.rows)
    assert view.instagram_status == stats_merge.IG_STATUS_OK


def test_performance_view_status_error_on_graph_failure(monkeypatch):
    """The crucial Slice 4 distinction: connected creator + Meta down
    must NOT silently look like 'manual only'. Status must be 'error'
    so the banner appears."""
    _patch_connected(monkeypatch)
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [{"week_start_date": "2026-06-01"}],
    )

    def _boom(token, **_kw):
        raise instagram_meta.InstagramError("graph 503")

    monkeypatch.setattr(instagram_meta, "get_user_media", _boom)

    view = stats_merge.performance_view("creator-1")
    # Manual row survives, IG row count is zero, but status flags the
    # outage so the template renders the unavailable banner.
    assert [r.source for r in view.rows] == ["manual"]
    assert view.instagram_status == stats_merge.IG_STATUS_ERROR


def test_performance_view_status_error_on_unexpected_exception(monkeypatch):
    """Defensive: any non-Graph oauth/storage exception during the
    precheck must also surface as 'error', never silently pass as
    'not connected'."""
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider": "instagram", "provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        oauth_connections, "instagram_account_id", lambda _c: "ig-1"
    )

    def _token_boom(_u):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(
        oauth_connections, "access_token_for_instagram", _token_boom
    )
    monkeypatch.setattr(performance, "list_for_user", lambda uid, **kw: [])

    view = stats_merge.performance_view("creator-1")
    assert view.instagram_status == stats_merge.IG_STATUS_ERROR


def test_combined_performance_still_returns_list(monkeypatch):
    """Back-compat pin: the existing list-returning surface must
    keep working for any caller that doesn't need the status."""
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    monkeypatch.setattr(
        performance,
        "list_for_user",
        lambda uid, **kw: [{"week_start_date": "2026-06-01"}],
    )
    result = stats_merge.combined_performance("creator-1")
    assert isinstance(result, list)
    assert all(isinstance(r, stats_merge.StatsRow) for r in result)


# ---------------------------------------------------------------------------
# instagram_account_snapshot — Phase B wrapper
# ---------------------------------------------------------------------------


def _empty_snapshot() -> dict[str, int | None]:
    return dict.fromkeys(
        instagram_meta.ACCOUNT_TOTAL_FIELDS
        + instagram_meta.ACCOUNT_INSIGHT_METRICS
    )


def test_account_snapshot_status_not_configured(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: False)
    snap, status = stats_merge.instagram_account_snapshot("creator-1")
    assert snap == _empty_snapshot()
    assert status == stats_merge.IG_STATUS_NOT_CONFIGURED


def test_account_snapshot_status_not_connected(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections, "get_instagram_connection", lambda _u: None
    )
    snap, status = stats_merge.instagram_account_snapshot("creator-1")
    assert snap == _empty_snapshot()
    assert status == stats_merge.IG_STATUS_NOT_CONNECTED


def test_account_snapshot_ok_flows_through(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        oauth_connections, "instagram_account_id", lambda _c: "ig-1"
    )
    monkeypatch.setattr(
        oauth_connections, "access_token_for_instagram", lambda _u: "TOKEN"
    )
    monkeypatch.setattr(
        instagram_meta,
        "get_account_snapshot",
        lambda token, **kw: {
            "followers_count": 5432,
            "follows_count": 210,
            "media_count": 88,
            "reach": 1200,
            "impressions": 1800,
            "profile_views": 40,
        },
    )
    snap, status = stats_merge.instagram_account_snapshot("creator-1")
    assert status == stats_merge.IG_STATUS_OK
    assert snap["followers_count"] == 5432
    assert snap["reach"] == 1200


def test_account_snapshot_graph_error_becomes_status_error(monkeypatch):
    monkeypatch.setattr(instagram_meta, "is_configured", lambda: True)
    monkeypatch.setattr(
        oauth_connections,
        "get_instagram_connection",
        lambda _u: {"provider_account_id": "ig-1"},
    )
    monkeypatch.setattr(
        oauth_connections, "instagram_account_id", lambda _c: "ig-1"
    )
    monkeypatch.setattr(
        oauth_connections, "access_token_for_instagram", lambda _u: "TOKEN"
    )

    def _boom(token, **kw):
        raise instagram_meta.InstagramError("graph 503")

    monkeypatch.setattr(instagram_meta, "get_account_snapshot", _boom)
    snap, status = stats_merge.instagram_account_snapshot("creator-1")
    assert status == stats_merge.IG_STATUS_ERROR
    assert snap == _empty_snapshot()
