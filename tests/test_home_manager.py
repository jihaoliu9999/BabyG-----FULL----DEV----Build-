"""Home V2 manager briefing read-model tests."""

from __future__ import annotations

from datetime import UTC, datetime

from app.services import home_manager


def _base_kwargs(**overrides):
    data = {
        "user_id": "creator-1",
        "google_connection": None,
        "instagram_connection": None,
        "instagram_snapshot": None,
        "instagram_growth": {},
        "latest_agent_cycle": None,
        "latest_sweep": None,
        "manager_activity": [],
        "unread_notifs": [],
        "pending_actions": [],
        "pending_connections": [],
        "upcoming_bookings": [],
        "matched_picks": [],
        "overnight_recap": None,
        "ig_dm_unread_count": 0,
        "open_deals": 0,
        "calendar_connected": False,
        "gmail_connected": False,
    }
    data.update(overrides)
    return data


def test_home_build_empty_state_uses_only_status_not_fake_alerts() -> None:
    home = home_manager.build(**_base_kwargs())

    assert home["clear"] is True
    assert home["needs_you"] == []
    assert home["brief"] == []
    assert [source["state"] for source in home["status"]["sources"]] == [
        "ready",
        "disconnected",
        "disconnected",
    ]


def test_home_build_keeps_instagram_dm_manager_alert_eligible() -> None:
    home = home_manager.build(
        **_base_kwargs(
            manager_activity=[
                {
                    "id": "ig-note-1",
                    "kind": "new_dm",
                    "title": "new instagram message from @brandco",
                    "body": "Potential campaign inquiry.",
                    "link_path": "/creator/instagram/dms?thread=thread-1",
                    "priority": "high",
                    "source_provider": "instagram",
                    "created_at": "2026-09-08T14:00:00Z",
                }
            ]
        )
    )

    assert home["clear"] is False
    assert home["needs_you"][0]["id"] == "ig-note-1"
    assert home["needs_you"][0]["source"] == "instagram"
    assert home["needs_you"][0]["href"] == "/creator/instagram/dms?thread=thread-1"


def test_home_status_does_not_say_just_now_ago() -> None:
    home = home_manager.build(
        **_base_kwargs(
            instagram_connection={"provider_account_id": "ig-1"},
            instagram_snapshot={"captured_at": datetime.now(UTC).isoformat()},
        )
    )

    assert home["status"]["headline"] == "checked just now"


def test_home_build_defensively_excludes_native_dm_manager_leaks() -> None:
    home = home_manager.build(
        **_base_kwargs(
            manager_activity=[
                {
                    "id": "native-note-1",
                    "kind": "new_dm",
                    "title": "new message from anna",
                    "body": "ordinary native DM",
                    "priority": "high",
                    "source_provider": "babyg",
                    "underlying_type": "dm_message",
                }
            ],
            unread_notifs=[
                {
                    "id": "native-note-2",
                    "kind": "new_dm",
                    "title": "new message from jo",
                    "priority": "urgent",
                    "source_provider": None,
                    "underlying_type": "dm_thread",
                }
            ],
        )
    )

    assert home["clear"] is True
    assert home["needs_you"] == []
    assert home["brief"] == []


def test_home_build_uses_real_recap_and_watching_counts() -> None:
    home = home_manager.build(
        **_base_kwargs(
            matched_picks=[{"id": "op-1", "card_kind": "opportunity", "title": "Studio match"}],
            overnight_recap={
                "counts": {
                    "proposals": 2,
                    "nudges": 1,
                    "cycles_active": 0,
                    "memory_writes": 1,
                    "ig_dms": 0,
                },
                "headlines": [],
            },
            open_deals=3,
            ig_dm_unread_count=2,
        )
    )

    assert home["handled"]["summary"] == "2 actions · 1 nudge · 1 memory update"
    assert home["watching"]["summary"] == (
        "watching 3 deals · 1 opportunity · 2 Instagram DMs"
    )
