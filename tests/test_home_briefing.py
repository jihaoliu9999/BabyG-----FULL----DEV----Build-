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


# ---- primary_carousel_slides ---------------------------------------


def _stub_no_native_dms(monkeypatch) -> None:
    """The composer reads native DMs via the `dms` service. Most tests
    below only care about action_proposals + notifications, so stub the
    native-DM path to return nothing."""
    monkeypatch.setattr(
        home_briefing.dms, "list_threads_for_user", lambda uid: []
    )
    monkeypatch.setattr(
        home_briefing.dms, "unread_counts_by_thread", lambda uid, ids: {}
    )
    monkeypatch.setattr(
        home_briefing.dms, "last_messages_by_thread", lambda ids: {}
    )


def test_carousel_zero_slides_when_nothing_actionable(monkeypatch) -> None:
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1", pending_actions=[], unread_notifs=[]
    )
    assert slides == []


def test_carousel_single_slide_for_one_action(monkeypatch) -> None:
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1",
        pending_actions=[
            {
                "id": "p-1",
                "action_type": "instagram.send_dm",
                "created_at": "2026-09-08T10:00:00Z",
                "preview": {"title": "reply to instagram dm", "body": "thanks"},
            }
        ],
        unread_notifs=[],
    )
    assert len(slides) == 1
    assert slides[0]["slide_type"] == "action_proposal"
    assert slides[0]["source"] == "instagram"


def test_carousel_ranks_high_stakes_action_before_normal(monkeypatch) -> None:
    """A gmail.send_email proposal (high-stakes) beats an older
    create_booking proposal (normal) even though the booking is older."""
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1",
        pending_actions=[
            {
                "id": "old",
                "action_type": "create_booking",
                "created_at": "2026-09-01T09:00:00Z",
                "preview": {"title": "old booking"},
            },
            {
                "id": "new",
                "action_type": "gmail.send_email",
                "created_at": "2026-09-08T09:00:00Z",
                "preview": {"title": "urgent email"},
            },
        ],
        unread_notifs=[],
    )
    # High-priority slide (gmail.send_email) comes first.
    assert slides[0]["primary_href"] == "/creator/bot#action-new"
    assert slides[1]["primary_href"] == "/creator/bot#action-old"


def test_carousel_never_infers_instagram_from_bare_new_dm(monkeypatch) -> None:
    """A `new_dm` notification with NO source_provider must NOT be
    presented as Instagram. Native babyg DMs surface via the dms
    helper; this notification is silently skipped."""
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1",
        pending_actions=[],
        unread_notifs=[
            {
                "id": "n-1",
                "kind": "new_dm",
                "title": "New message",
                "body": "hi",
                "link_path": "/creator/dm/some-thread",
                # No source_provider — ambiguous, must be skipped.
            }
        ],
    )
    assert slides == []


def test_carousel_surfaces_instagram_dm_when_source_provider_set(
    monkeypatch,
) -> None:
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1",
        pending_actions=[],
        unread_notifs=[
            {
                "id": "n-ig",
                "kind": "new_dm",
                "title": "New message from @brand",
                "body": "want to collab",
                "link_path": "/creator/instagram/dms#thread-x",
                "source_provider": "instagram",
                "priority": "high",
                "created_at": "2026-09-08T12:00:00Z",
            }
        ],
    )
    assert len(slides) == 1
    assert slides[0]["source"] == "instagram"
    assert slides[0]["primary_href"] == "/creator/instagram/dms#thread-x"


def test_carousel_surfaces_native_babyg_dm_as_native(monkeypatch) -> None:
    """Unread native DMs come from `dms` service and stay source='babyg'."""
    monkeypatch.setattr(
        home_briefing.dms, "list_threads_for_user",
        lambda uid: [
            {"id": "t-1", "peer_id": "peer-1",
             "last_message_at": "2026-09-08T11:00:00Z",
             "participant_a_id": "c1", "participant_b_id": "peer-1"},
        ],
    )
    monkeypatch.setattr(
        home_briefing.dms, "unread_counts_by_thread",
        lambda uid, ids: {"t-1": 2},
    )
    monkeypatch.setattr(
        home_briefing.dms, "last_messages_by_thread",
        lambda ids: {"t-1": {"body": "hey", "created_at": "2026-09-08T11:00:00Z"}},
    )
    monkeypatch.setattr(
        home_briefing.profiles, "get_creator_profile",
        lambda uid: {"full_name": "Alex"},
    )
    slides = home_briefing.primary_carousel_slides(
        "c1", pending_actions=[], unread_notifs=[]
    )
    assert len(slides) == 1
    assert slides[0]["slide_type"] == "native_dm"
    assert slides[0]["source"] == "babyg"  # NEVER instagram
    assert slides[0]["primary_href"] == "/creator/dm/t-1"
    assert "Alex" in slides[0]["title"]


def test_carousel_native_dm_falls_back_to_someone(monkeypatch) -> None:
    monkeypatch.setattr(
        home_briefing.dms, "list_threads_for_user",
        lambda uid: [
            {"id": "t-1", "peer_id": "peer-1",
             "last_message_at": "2026-09-08T11:00:00Z",
             "participant_a_id": "c1", "participant_b_id": "peer-1"},
        ],
    )
    monkeypatch.setattr(
        home_briefing.dms, "unread_counts_by_thread",
        lambda uid, ids: {"t-1": 1},
    )
    monkeypatch.setattr(
        home_briefing.dms, "last_messages_by_thread",
        lambda ids: {"t-1": {"body": "hi", "created_at": "2026-09-08T11:00:00Z"}},
    )
    monkeypatch.setattr(
        home_briefing.profiles, "get_creator_profile", lambda uid: {}
    )
    slides = home_briefing.primary_carousel_slides(
        "c1", pending_actions=[], unread_notifs=[]
    )
    assert slides[0]["title"] == "New message from someone"


def test_carousel_caps_at_max_slides(monkeypatch) -> None:
    _stub_no_native_dms(monkeypatch)
    many_actions = [
        {
            "id": f"p-{i}",
            "action_type": "gmail.create_draft",
            "created_at": f"2026-09-01T{i:02d}:00:00Z",
            "preview": {"title": f"draft {i}"},
        }
        for i in range(10)
    ]
    slides = home_briefing.primary_carousel_slides(
        "c1", pending_actions=many_actions, unread_notifs=[]
    )
    assert len(slides) == home_briefing.CAROUSEL_MAX_SLIDES


def test_carousel_notification_missing_link_path_skipped(monkeypatch) -> None:
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1",
        pending_actions=[],
        unread_notifs=[
            {"id": "n-1", "kind": "system", "title": "no link"},
        ],
    )
    assert slides == []


def test_carousel_urgent_priority_wins_over_high(monkeypatch) -> None:
    _stub_no_native_dms(monkeypatch)
    slides = home_briefing.primary_carousel_slides(
        "c1",
        pending_actions=[],
        unread_notifs=[
            {
                "id": "high", "kind": "manager_alert",
                "title": "high one", "link_path": "/x",
                "source_provider": "instagram", "priority": "high",
                "created_at": "2026-09-08T13:00:00Z",
            },
            {
                "id": "urgent", "kind": "manager_alert",
                "title": "urgent one", "link_path": "/y",
                "source_provider": "instagram", "priority": "urgent",
                "created_at": "2026-09-08T12:00:00Z",
            },
        ],
    )
    assert slides[0]["title"] == "urgent one"
    assert slides[1]["title"] == "high one"
