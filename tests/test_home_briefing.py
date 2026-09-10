"""Tests for the home v5 briefing composer.

Contract:
  * connected_count is the sum of {ig connected + gmail scope + calendar scope}
  * a needs_reconnect provider does NOT count as connected
  * primary_manager_update prefers pending action_proposals over notifs
  * brief_rows never fabricates a row; empty sources yield an empty list
  * brief_rows caps at BRIEF_MAX (3) even when many sources fire
  * handled_today swallows supabase errors -> 0
  * watching_summary composes deals + opportunities from real state
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import home_briefing


class _FakePerformanceView:
    def __init__(self, rows):
        self.rows = rows


class _FakeIgRow:
    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


# ---- integration_status ---------------------------------------------


def _stub_oauth(
    monkeypatch,
    *,
    ig_connected=False,
    ig_needs_reconnect=False,
    gmail_connected=False,
    calendar_connected=False,
):
    monkeypatch.setattr(
        home_briefing.oauth_connections,
        "get_google_connection",
        lambda uid: {"connected": True} if (
            gmail_connected or calendar_connected
        ) else None,
    )
    monkeypatch.setattr(
        home_briefing.oauth_connections,
        "get_instagram_connection",
        lambda uid: {"access_token": "tok"} if ig_connected else None,
    )
    monkeypatch.setattr(
        home_briefing.oauth_connections,
        "google_gmail_connected",
        lambda conn: bool(gmail_connected),
    )
    monkeypatch.setattr(
        home_briefing.oauth_connections,
        "google_calendar_connected",
        lambda conn: bool(calendar_connected),
    )
    monkeypatch.setattr(
        home_briefing.oauth_connections,
        "instagram_needs_reconnect",
        lambda uid: bool(ig_needs_reconnect),
    )


def test_integration_status_zero_connected(monkeypatch) -> None:
    _stub_oauth(monkeypatch)
    result = home_briefing.integration_status("c1")
    assert result["connected_count"] == 0
    assert len(result["rows"]) == 3
    for row in result["rows"]:
        assert row["connected"] is False
        assert row["action_label"] == "connect"


def test_integration_status_counts_working_providers(monkeypatch) -> None:
    _stub_oauth(monkeypatch, ig_connected=True, gmail_connected=True)
    result = home_briefing.integration_status("c1")
    assert result["connected_count"] == 2


def test_integration_status_excludes_needs_reconnect(monkeypatch) -> None:
    _stub_oauth(monkeypatch, ig_connected=True, ig_needs_reconnect=True)
    result = home_briefing.integration_status("c1")
    assert result["connected_count"] == 0
    ig_row = next(r for r in result["rows"] if r["slot"] == "instagram")
    assert ig_row["needs_reconnect"] is True
    assert ig_row["action_label"] == "reconnect"


def test_integration_status_all_three_connected(monkeypatch) -> None:
    _stub_oauth(
        monkeypatch,
        ig_connected=True,
        gmail_connected=True,
        calendar_connected=True,
    )
    result = home_briefing.integration_status("c1")
    assert result["connected_count"] == 3
    for row in result["rows"]:
        assert row["connected"] is True
        assert row["action_label"] == "open"


# ---- primary_manager_update -----------------------------------------


def test_primary_prefers_pending_action_over_notification() -> None:
    result = home_briefing.primary_manager_update(
        pending_actions=[
            {
                "id": "p-1",
                "action_type": "instagram.send_dm",
                "created_at": "2026-09-08T10:00:00Z",
                "preview": {"title": "reply to instagram dm", "body": "thanks"},
            }
        ],
        unread_notifs=[
            {
                "id": "n-1",
                "kind": "connection_request",
                "title": "someone wants to connect",
                "link_path": "/creator/connections",
            }
        ],
    )
    assert result is not None
    assert result["source"] == "instagram"
    assert result["primary_href"] == "/creator/bot#action-p-1"


def test_primary_falls_back_to_first_notification() -> None:
    result = home_briefing.primary_manager_update(
        pending_actions=[],
        unread_notifs=[
            {
                "id": "n-1",
                "kind": "connection_request",
                "title": "new connection request",
                "body": None,
                "link_path": "/creator/connections",
            }
        ],
    )
    assert result is not None
    assert result["source"] == "babyg"
    assert result["primary_href"] == "/creator/connections"


def test_primary_returns_none_when_nothing_pending() -> None:
    assert home_briefing.primary_manager_update(
        pending_actions=[], unread_notifs=[]
    ) is None
    assert home_briefing.primary_manager_update(
        pending_actions=None, unread_notifs=None
    ) is None


# ---- brief_rows -----------------------------------------------------


def test_brief_empty_when_no_real_signals() -> None:
    rows = home_briefing.brief_rows(
        matched_picks=[],
        ig_dm_unread_count=0,
        overnight_recap=None,
        performance_view=None,
    )
    assert rows == []


def test_brief_includes_ig_unread_row() -> None:
    rows = home_briefing.brief_rows(
        matched_picks=[],
        ig_dm_unread_count=3,
        overnight_recap=None,
        performance_view=None,
    )
    assert any("instagram" in r["slot"] for r in rows)


def test_brief_caps_at_three() -> None:
    rows = home_briefing.brief_rows(
        matched_picks=[
            {"card_id": "op-1", "card_kind": "opportunity", "title": "Op A"},
        ],
        ig_dm_unread_count=1,
        overnight_recap={
            "headlines": [
                "ran 2 thinking cycles",
                "updated your memory 1 time",
                "another headline",
                "yet another",
            ],
        },
        performance_view=_FakePerformanceView(rows=[_FakeIgRow(reach=1200)]),
    )
    assert len(rows) == home_briefing.BRIEF_MAX == 3


def test_brief_skips_headline_already_covered() -> None:
    rows = home_briefing.brief_rows(
        matched_picks=[],
        ig_dm_unread_count=2,  # will produce an instagram row
        overnight_recap={
            "headlines": [
                "caught 2 new instagram dms",  # dedup — instagram already shown
                "ran 1 thinking cycle",
            ],
        },
        performance_view=None,
    )
    # The recap headline about IG DMs must not appear as a duplicate row.
    assert not any("caught 2 new instagram dms" in r["title"] for r in rows)


# ---- handled_today --------------------------------------------------


def test_handled_today_swallows_supabase_error(monkeypatch) -> None:
    class _BoomClient:
        def table(self, _):
            raise RuntimeError("supabase down")

    monkeypatch.setattr(
        home_briefing.supabase_client,
        "get_service_client",
        lambda: _BoomClient(),
    )
    assert home_briefing.handled_today("c1") == 0


def test_handled_today_counts_rows(monkeypatch) -> None:
    class _StubClient:
        def table(self, name):
            return _StubTable()

    class _StubTable:
        def select(self, *_): return self
        def eq(self, *_): return self
        def in_(self, *_): return self
        def gte(self, *_): return self
        def limit(self, *_): return self
        def execute(self): return type("R", (), {"data": [{"id": "1"}, {"id": "2"}, {"id": "3"}]})()

    monkeypatch.setattr(
        home_briefing.supabase_client,
        "get_service_client",
        lambda: _StubClient(),
    )
    assert home_briefing.handled_today(
        "c1", now=datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    ) == 3


# ---- watching_summary -----------------------------------------------


def test_watching_summary_composes_from_state() -> None:
    result = home_briefing.watching_summary(
        matched_picks=[
            {"card_id": "op-1", "card_kind": "opportunity"},
            {"card_id": "b-1", "card_kind": "brand"},
        ],
        pending_actions_all=[{"id": "p-1"}, {"id": "p-2"}, {"id": "p-3"}],
    )
    assert result == {"deals": 3, "opportunities": 1}


def test_watching_summary_zero_state() -> None:
    assert home_briefing.watching_summary(
        matched_picks=None, pending_actions_all=None
    ) == {"deals": 0, "opportunities": 0}


# ---- relative_ago ---------------------------------------------------


def test_relative_ago_parses_iso() -> None:
    ago = home_briefing.relative_ago(
        "2026-09-08T13:00:00Z",
        now=datetime(2026, 9, 8, 15, 0, tzinfo=UTC),
    )
    assert ago == "2h ago"


def test_relative_ago_empty_on_bad_input() -> None:
    assert home_briefing.relative_ago("") == ""
    assert home_briefing.relative_ago(None) == ""
    assert home_briefing.relative_ago("not a timestamp") == ""
