"""Content receipts + weekly performance log tests."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import abuse as abuse_module
from app.services import dms as dms_module
from app.services import intel as intel_module
from app.services import notifications as notifications_module
from app.services import performance as performance_module
from app.services import receipts as receipts_module


class FakeWorld:
    def __init__(self):
        self.receipts: dict[str, dict[str, Any]] = {}
        self.perf: dict[tuple[str, str], dict[str, Any]] = {}


@pytest.fixture()
def world(monkeypatch) -> FakeWorld:
    w = FakeWorld()

    def _list_receipts(uid, *, limit=100):
        rows = [r for r in w.receipts.values() if r["user_id"] == uid]
        return rows

    def _create_receipt(*, user_id, payload):
        rid = str(uuid4())
        w.receipts[rid] = {**payload, "id": rid, "user_id": user_id}
        return rid

    monkeypatch.setattr(receipts_module, "list_for_user", _list_receipts)
    monkeypatch.setattr(receipts_module, "create", _create_receipt)

    def _list_perf(uid, *, limit=26):
        return [p for (u, _), p in w.perf.items() if u == uid]

    def _upsert_perf(*, user_id, entered_by, payload):
        wk = payload["week_start_date"]
        w.perf[(user_id, wk)] = {**payload, "user_id": user_id, "entered_by_user_id": entered_by}
        return True

    monkeypatch.setattr(performance_module, "list_for_user", _list_perf)
    monkeypatch.setattr(performance_module, "upsert", _upsert_perf)
    monkeypatch.setattr(performance_module, "last_monday_iso", lambda: "2026-05-04")

    monkeypatch.setattr(notifications_module, "create", lambda **kw: True)
    monkeypatch.setattr(notifications_module, "list_unread", lambda uid, *, limit=10: [])
    monkeypatch.setattr(notifications_module, "unread_count", lambda uid: 0)
    monkeypatch.setattr(dms_module, "unread_count_for_user", lambda uid: 0)
    monkeypatch.setattr(intel_module, "feed_for_creator", lambda **kw: [])
    monkeypatch.setattr(intel_module, "feedback_for_user", lambda uid, ids: {})
    monkeypatch.setattr(abuse_module, "count_pending", lambda: 0)
    return w


@pytest.fixture()
def client():
    return TestClient(app, follow_redirects=False)


def _signed_in(client, *, role, user_id):
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": role})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


# Receipts

def test_receipts_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    rid = str(uuid4())
    world.receipts[rid] = {
        "id": rid, "user_id": "c-1",
        "post_url": "https://example.com/p/1", "post_type": "reel",
        "caption_excerpt": "Hello world", "likes_count": 100,
        "comments_count": 5, "posted_at": "2026-05-07T00:00:00Z",
    }
    r = client.get("/creator/receipts")
    assert r.status_code == 200
    assert "Hello world" in r.text


def test_receipts_create(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/receipts",
        data={
            "post_url": "https://example.com/p/2",
            "post_type": "carousel",
            "caption_excerpt": "Caption.",
            "likes_count": "500",
            "comments_count": "12",
            "posted_at": "2026-05-07T10:00",
        },
    )
    assert r.status_code == 303
    assert len(world.receipts) == 1
    rec = next(iter(world.receipts.values()))
    assert rec["likes_count"] == 500


def test_receipts_rejects_bad_likes(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/receipts",
        data={
            "post_url": "https://example.com/p/3",
            "post_type": "reel",
            "likes_count": "abc",
        },
    )
    assert r.status_code == 400
    assert "whole number" in r.text.lower()


def test_receipts_requires_url(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/receipts",
        data={"post_type": "reel"},
    )
    assert r.status_code == 400


def test_receipts_requires_creator(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/receipts")
    assert r.status_code == 403


# Performance

def test_performance_list_renders(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    world.perf[("c-1", "2026-05-04")] = {
        "user_id": "c-1", "week_start_date": "2026-05-04",
        "engagement_rate": 4.2, "follower_delta": 120,
        "active_brand_deals_count": 2, "active_brand_deals_value": 5000,
        "notes": None,
    }
    r = client.get("/creator/performance")
    assert r.status_code == 200
    assert "2026-05-04" in r.text


def test_performance_upsert(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/performance",
        data={
            "week_start_date": "2026-05-04",
            "engagement_rate": "4.2",
            "follower_delta": "120",
            "active_brand_deals_count": "2",
            "active_brand_deals_value": "5000",
            "notes": "Strong reel performance.",
        },
    )
    assert r.status_code == 303
    assert ("c-1", "2026-05-04") in world.perf
    p = world.perf[("c-1", "2026-05-04")]
    assert p["engagement_rate"] == 4.2
    assert p["follower_delta"] == 120


def test_performance_requires_week(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post("/creator/performance", data={"engagement_rate": "1.0"})
    assert r.status_code == 400


def test_performance_rejects_bad_engagement(client, world):
    _signed_in(client, role="creator", user_id="c-1")
    r = client.post(
        "/creator/performance",
        data={"week_start_date": "2026-05-04", "engagement_rate": "high"},
    )
    assert r.status_code == 400


def test_performance_requires_creator(client, world):
    _signed_in(client, role="operator", user_id="op-1")
    r = client.get("/creator/performance")
    assert r.status_code == 403


# Performance — Instagram + manual merge


def test_performance_list_shows_manual_pill_when_ig_not_configured(client, world):
    """The page-foot copy stays at the safe default and rows carry the
    manual pill when the app has no IG creds at all."""
    _signed_in(client, role="creator", user_id="c-1")
    world.perf[("c-1", "2026-05-04")] = {
        "user_id": "c-1", "week_start_date": "2026-05-04",
        "engagement_rate": 4.2, "notes": None,
    }
    r = client.get("/creator/performance")
    assert r.status_code == 200
    # Source pill is on the row.
    assert "manual" in r.text
    # Adaptive footer copy: no IG configured → original line stays.
    assert "auto-sync requires platform api access" in r.text


def test_performance_list_merges_instagram_rows(client, world, monkeypatch):
    """When performance_view surfaces both sources with status=ok, both
    render, the page-foot copy reflects live IG, and the engagement
    bar chart appears above the cards."""
    from app.services import stats_merge as merge_module

    _signed_in(client, role="creator", user_id="c-1")

    fake_rows = [
        merge_module.StatsRow(
            source="instagram",
            title="dinner at boia",
            timestamp="2026-06-08T18:00:00+0000",
            permalink="https://www.instagram.com/p/m1",
            metrics={"likes": 120, "comments": 4, "reach": 900, "engagement": 50},
            notes=None,
        ),
        merge_module.StatsRow(
            source="instagram",
            title="brickell sunset",
            timestamp="2026-06-05T18:00:00+0000",
            permalink="https://www.instagram.com/p/m2",
            metrics={"likes": 80, "comments": 2, "engagement": 30},
            notes=None,
        ),
        merge_module.StatsRow(
            source="manual",
            title="week of 2026-06-01",
            timestamp="2026-06-01",
            permalink=None,
            metrics={"engagement_rate": 4.2, "follower_delta": 120},
            notes="good week",
        ),
    ]
    monkeypatch.setattr(
        merge_module,
        "performance_view",
        lambda uid, **kw: merge_module.PerformanceView(
            rows=fake_rows, instagram_status=merge_module.IG_STATUS_OK
        ),
    )

    r = client.get("/creator/performance")
    assert r.status_code == 200
    assert "dinner at boia" in r.text
    assert "week of 2026-06-01" in r.text
    # IG permalink rendered as "view post" link.
    assert "https://www.instagram.com/p/m1" in r.text
    assert "view post" in r.text
    # Both source pills appear.
    assert "manual" in r.text
    assert "instagram" in r.text
    # Adaptive footer reflects connected state.
    assert "manual + instagram" in r.text
    # Bar chart renders for IG rows only when engagement values exist.
    assert "engagement by post" in r.text


def test_performance_list_prompts_connect_when_configured_but_not_connected(
    client, world, monkeypatch
):
    """The mid-state: app has IG creds, this creator hasn't linked.
    The footer should invite them to connect."""
    from app.services import stats_merge as merge_module

    _signed_in(client, role="creator", user_id="c-1")
    monkeypatch.setattr(
        merge_module,
        "performance_view",
        lambda uid, **kw: merge_module.PerformanceView(
            rows=[], instagram_status=merge_module.IG_STATUS_NOT_CONNECTED
        ),
    )

    r = client.get("/creator/performance")
    assert r.status_code == 200
    assert "connect instagram" in r.text.lower()
    assert "/creator/profile/settings" in r.text


def test_performance_list_shows_unavailable_banner_on_error(
    client, world, monkeypatch
):
    """Connected creator + Meta API failing: the banner must surface
    and the footer must drop the misleading 'live merged' copy."""
    from app.services import stats_merge as merge_module

    _signed_in(client, role="creator", user_id="c-1")
    manual_row = merge_module.StatsRow(
        source="manual",
        title="week of 2026-06-01",
        timestamp="2026-06-01",
        permalink=None,
        metrics={"engagement_rate": 4.2},
        notes=None,
    )
    monkeypatch.setattr(
        merge_module,
        "performance_view",
        lambda uid, **kw: merge_module.PerformanceView(
            rows=[manual_row], instagram_status=merge_module.IG_STATUS_ERROR
        ),
    )

    r = client.get("/creator/performance")
    assert r.status_code == 200
    assert "instagram unavailable" in r.text.lower()
    assert "temporarily unavailable" in r.text
    # Footer reflects the outage, not the merged-state copy.
    assert "instagram live data unavailable" in r.text
    assert "manual + instagram" not in r.text
    # The chart should NOT render when there are no IG rows.
    assert "engagement by post" not in r.text
