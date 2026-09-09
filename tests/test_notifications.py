"""Notification service tests."""

from __future__ import annotations

from app.services import notifications


class _Result:
    def __init__(self, data):
        self.data = data


class _FakeNotificationsTable:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.filters: list[tuple[str, str, object]] = []
        self.or_filter: str | None = None
        self.limit_value: int | None = None

    def select(self, _cols, **_kwargs):
        return self

    def eq(self, col, val):
        self.filters.append(("eq", col, val))
        return self

    def is_(self, col, val):
        self.filters.append(("is", col, val))
        return self

    def or_(self, value):
        self.or_filter = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.limit_value = value
        return self

    def execute(self):
        return _Result(self.rows)


class _FakeClient:
    def __init__(self, table):
        self._table = table

    def table(self, name):
        assert name == "notifications"
        return self._table


def test_list_manager_activity_excludes_native_dm_notifications(monkeypatch) -> None:
    table = _FakeNotificationsTable([
        {
            "id": "native-dm",
            "kind": "new_dm",
            "title": "New message from Anna",
            "source_provider": None,
            "underlying_type": None,
        },
        {
            "id": "native-dm-explicit",
            "kind": "new_dm",
            "title": "New message from Jo",
            "source_provider": "babyg",
            "underlying_type": "dm_message",
        },
        {
            "id": "instagram-dm",
            "kind": "new_dm",
            "title": "new instagram message from @brandco",
            "source_provider": "instagram",
            "underlying_type": "instagram_dm_message",
        },
        {
            "id": "profile-sync",
            "kind": "profile_sync",
            "title": "instagram profile changed",
            "source_provider": "instagram",
        },
    ])
    monkeypatch.setattr(
        notifications.supabase_client,
        "get_service_client",
        lambda: _FakeClient(table),
    )

    rows = notifications.list_manager_activity("creator-1", limit=5)

    assert [row["id"] for row in rows] == ["instagram-dm", "profile-sync"]
    assert (
        table.or_filter
        == "kind.in.(manager_alert,profile_sync,performance_spike),"
        "and(kind.eq.new_dm,source_provider.eq.instagram)"
    )
    assert ("eq", "user_id", "creator-1") in table.filters
    assert ("eq", "is_read", False) in table.filters
    assert ("is", "archived_at", "null") in table.filters


def test_list_manager_activity_keeps_limit_after_filtering(monkeypatch) -> None:
    table = _FakeNotificationsTable([
        {
            "id": "instagram-dm",
            "kind": "new_dm",
            "source_provider": "instagram",
        },
        {
            "id": "manager-alert",
            "kind": "manager_alert",
            "source_provider": None,
        },
    ])
    monkeypatch.setattr(
        notifications.supabase_client,
        "get_service_client",
        lambda: _FakeClient(table),
    )

    rows = notifications.list_manager_activity("creator-1", limit=1)

    assert [row["id"] for row in rows] == ["instagram-dm"]
    assert table.limit_value == 1
