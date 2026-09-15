"""Real-data Brief aggregation tests.

These tests keep the approved Brief UI separate from the data bridge:
the service consumes persisted, user-scoped rows and never falls back to
prototype/provider-global content.
"""

from __future__ import annotations

from typing import Any

from app.services import brief


class _Result:
    def __init__(self, data: list[dict[str, Any]]):
        self.data = data


class _FakeQuery:
    def __init__(self, table: str, rows: list[dict[str, Any]]):
        self.table = table
        self.rows = rows
        self.filters: dict[str, Any] = {}
        self.in_filters: dict[str, set[Any]] = {}
        self.limit_value = 100

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def in_(self, key, values):
        self.in_filters[key] = set(values)
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, value):
        self.limit_value = int(value)
        return self

    def execute(self):
        rows = [
            row
            for row in self.rows
            if all(row.get(key) == value for key, value in self.filters.items())
            and all(row.get(key) in values for key, values in self.in_filters.items())
        ]
        return _Result(rows[: self.limit_value])


class _FakeSupabase:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self.tables = tables
        self.queries: list[tuple[str, dict[str, Any]]] = []

    def table(self, name: str):
        query = _FakeQuery(name, self.tables.get(name, []))
        original_execute = query.execute

        def _execute():
            self.queries.append((name, dict(query.filters)))
            return original_execute()

        query.execute = _execute  # type: ignore[method-assign]
        return query


def _connections(
    monkeypatch,
    *,
    gmail: bool = False,
    instagram: bool = False,
    calendar: bool = False,
) -> None:
    monkeypatch.setattr(
        brief.oauth_connections,
        "get_google_connection",
        lambda user_id: {"user_id": user_id} if (gmail or calendar) else None,
    )
    monkeypatch.setattr(
        brief.oauth_connections,
        "get_instagram_connection",
        lambda user_id: {
            "user_id": user_id,
            "access_token": "token-present",
            "provider_account_id": f"ig-{user_id}",
        }
        if instagram
        else None,
    )
    monkeypatch.setattr(
        brief.oauth_connections,
        "google_gmail_connected",
        lambda connection: bool(gmail),
    )
    monkeypatch.setattr(
        brief.oauth_connections,
        "google_gmail_compose_connected",
        lambda connection: bool(gmail),
    )
    monkeypatch.setattr(
        brief.oauth_connections,
        "google_gmail_send_connected",
        lambda connection: bool(gmail),
    )
    monkeypatch.setattr(
        brief.oauth_connections,
        "google_calendar_connected",
        lambda connection: bool(calendar),
    )


def _proposal(user_id: str, *, title: str = "draft reply to BrandCo") -> dict[str, Any]:
    return {
        "id": f"proposal-{user_id}",
        "user_id": user_id,
        "action_type": "gmail.create_draft",
        "provider": "google",
        "preview": {
            "title": title,
            "subject": "paid campaign",
            "to": "partner@brand.test",
        },
        "created_at": "2026-09-14T12:00:00Z",
    }


def _instagram_notification(user_id: str, *, title: str = "@brand asked for rates") -> dict[str, Any]:
    return {
        "id": f"notif-{user_id}",
        "user_id": user_id,
        "kind": "new_dm",
        "title": title,
        "body": "Clarify scope and usage before quoting.",
        "source_provider": "instagram",
        "source_thread_id": f"thread-{user_id}",
        "underlying_type": "instagram_dm_message",
        "underlying_id": f"message-{user_id}",
        "metadata": {"matter_type": "deal"},
        "link_path": f"/creator/instagram/dms?thread=thread-{user_id}",
        "is_read": False,
        "priority": "high",
        "created_at": "2026-09-14T13:00:00Z",
    }


def _stub_rows(
    monkeypatch,
    *,
    proposals: dict[str, list[dict[str, Any]]] | None = None,
    notifications: dict[str, list[dict[str, Any]]] | None = None,
    tables: dict[str, list[dict[str, Any]]] | None = None,
) -> _FakeSupabase:
    proposal_calls: list[str] = []
    notification_calls: list[str] = []

    def _list_pending_for_user(*, user_id: str, limit: int = 20):
        proposal_calls.append(user_id)
        return list((proposals or {}).get(user_id, []))

    def _list_for_user(user_id: str, *, limit: int = 50, include_archived: bool = False):
        notification_calls.append(user_id)
        return list((notifications or {}).get(user_id, []))

    monkeypatch.setattr(brief.action_proposals, "list_pending_for_user", _list_pending_for_user)
    monkeypatch.setattr(brief.notifications, "list_for_user", _list_for_user)
    fake = _FakeSupabase(tables or {})
    monkeypatch.setattr(brief.supabase_client, "get_service_client", lambda: fake)
    fake.proposal_calls = proposal_calls  # type: ignore[attr-defined]
    fake.notification_calls = notification_calls  # type: ignore[attr-defined]
    return fake


def test_gmail_only_user_gets_real_gmail_matter(monkeypatch) -> None:
    _connections(monkeypatch, gmail=True)
    fake = _stub_rows(monkeypatch, proposals={"user-a": [_proposal("user-a")]})

    view = brief.build_brief("user-a")

    assert [card["platform"] for card in view["cards"]] == ["gmail"]
    assert "draft reply to BrandCo" in view["cards"][0]["headline"]
    assert fake.proposal_calls == ["user-a"]  # type: ignore[attr-defined]


def test_instagram_only_user_gets_real_instagram_notification(monkeypatch) -> None:
    _connections(monkeypatch, instagram=True)
    _stub_rows(monkeypatch, notifications={"user-a": [_instagram_notification("user-a")]})

    view = brief.build_brief("user-a")

    assert [card["platform"] for card in view["cards"]] == ["instagram"]
    assert view["cards"][0]["platform_label"] == "Instagram"
    assert view["cards"][0]["matter_type"] == "deal"


def test_both_connected_combines_into_one_prioritized_feed(monkeypatch) -> None:
    _connections(monkeypatch, gmail=True, instagram=True)
    _stub_rows(
        monkeypatch,
        proposals={"user-a": [_proposal("user-a")]},
        notifications={"user-a": [_instagram_notification("user-a")]},
    )

    view = brief.build_brief("user-a")

    assert {card["platform"] for card in view["cards"]} == {"gmail", "instagram"}
    assert len(view["cards"]) == 2


def test_multi_user_isolation_for_gmail_instagram_and_evaluations(monkeypatch) -> None:
    _connections(monkeypatch, gmail=True, instagram=True)
    fake = _stub_rows(
        monkeypatch,
        proposals={
            "user-a": [_proposal("user-a", title="draft reply to Alpha")],
            "user-b": [_proposal("user-b", title="draft reply to Beta")],
        },
        notifications={
            "user-a": [_instagram_notification("user-a", title="@alpha asked for rates")],
            "user-b": [_instagram_notification("user-b", title="@beta asked for usage")],
        },
        tables={
            "instagram_dm_evaluations": [
                {
                    "id": "eval-a",
                    "creator_id": "user-a",
                    "thread_id": "thread-user-a",
                    "message_id": "msg-a",
                    "result": {
                        "Summary": "Alpha wants a paid collaboration",
                        "Worth responding?": "yes",
                    },
                    "created_at": "2026-09-14T14:00:00Z",
                },
                {
                    "id": "eval-b",
                    "creator_id": "user-b",
                    "thread_id": "thread-user-b",
                    "message_id": "msg-b",
                    "result": {
                        "Summary": "Beta wants a paid collaboration",
                        "Worth responding?": "yes",
                    },
                    "created_at": "2026-09-14T14:00:00Z",
                },
            ],
            "instagram_dm_threads": [
                {"id": "thread-user-a", "creator_id": "user-a", "peer_username": "alpha"},
                {"id": "thread-user-b", "creator_id": "user-b", "peer_username": "beta"},
            ],
        },
    )

    user_a = brief.build_brief("user-a")
    user_b = brief.build_brief("user-b")
    text_a = " ".join(card["headline"] for card in user_a["cards"]).lower()
    text_b = " ".join(card["headline"] for card in user_b["cards"]).lower()

    assert "alpha" in text_a
    assert "beta" not in text_a
    assert "beta" in text_b
    assert "alpha" not in text_b
    assert fake.proposal_calls == ["user-a", "user-b"]  # type: ignore[attr-defined]
    assert fake.notification_calls == ["user-a", "user-b"]  # type: ignore[attr-defined]
    assert ("instagram_dm_evaluations", {"creator_id": "user-a"}) in fake.queries
    assert ("instagram_dm_evaluations", {"creator_id": "user-b"}) in fake.queries


def test_neither_connected_has_no_provider_cards(monkeypatch) -> None:
    _connections(monkeypatch)
    _stub_rows(
        monkeypatch,
        proposals={"user-a": [_proposal("user-a")]},
        notifications={"user-a": [_instagram_notification("user-a")]},
    )

    view = brief.build_brief("user-a")

    assert view["cards"] == []
    assert view["has_connected_provider"] is False


def test_connected_provider_with_zero_meaningful_matters_is_empty_not_disconnected(monkeypatch) -> None:
    _connections(monkeypatch, gmail=True, instagram=True)
    _stub_rows(monkeypatch)

    view = brief.build_brief("user-a")

    assert view["cards"] == []
    assert view["empty"] is True
    assert view["has_connected_provider"] is True


def test_one_provider_error_does_not_drop_other_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        brief.oauth_connections,
        "get_google_connection",
        lambda user_id: (_ for _ in ()).throw(RuntimeError("google down")),
    )
    monkeypatch.setattr(
        brief.oauth_connections,
        "get_instagram_connection",
        lambda user_id: {
            "access_token": "token-present",
            "provider_account_id": f"ig-{user_id}",
        },
    )
    _stub_rows(monkeypatch, notifications={"user-a": [_instagram_notification("user-a")]})

    view = brief.build_brief("user-a")

    assert [card["platform"] for card in view["cards"]] == ["instagram"]
    assert view["connections"]["gmail"]["error"] is True


def test_meaningful_instagram_evaluation_surfaces(monkeypatch) -> None:
    _connections(monkeypatch, instagram=True)
    _stub_rows(
        monkeypatch,
        tables={
            "instagram_dm_evaluations": [
                {
                    "id": "eval-a",
                    "creator_id": "user-a",
                    "thread_id": "thread-a",
                    "message_id": "msg-a",
                    "result": {
                        "Summary": "Brand wants rates for two reels",
                        "Worth responding?": "yes",
                        "Opportunity": "Potential paid collaboration.",
                        "Suggested next steps": "Ask for usage rights and timeline.",
                    },
                    "created_at": "2026-09-14T14:00:00Z",
                }
            ],
            "instagram_dm_threads": [
                {"id": "thread-a", "creator_id": "user-a", "peer_username": "brandco"}
            ],
        },
    )

    view = brief.build_brief("user-a")

    assert view["cards"][0]["platform"] == "instagram"
    assert view["cards"][0]["matter_type"] == "deal"
    assert "@brandco" in view["cards"][0]["headline"]


def test_irrelevant_instagram_evaluation_is_not_surfaced(monkeypatch) -> None:
    _connections(monkeypatch, instagram=True)
    _stub_rows(
        monkeypatch,
        tables={
            "instagram_dm_evaluations": [
                {
                    "id": "eval-a",
                    "creator_id": "user-a",
                    "thread_id": "thread-a",
                    "message_id": "msg-a",
                    "result": {
                        "Summary": "Follower sent a casual compliment",
                        "Worth responding?": "no",
                    },
                    "created_at": "2026-09-14T14:00:00Z",
                }
            ]
        },
    )

    assert brief.build_brief("user-a")["cards"] == []


def test_unknown_source_is_excluded_not_mapped_to_babyg(monkeypatch) -> None:
    _connections(monkeypatch, gmail=True, instagram=True)
    _stub_rows(
        monkeypatch,
        notifications={
            "user-a": [
                {
                    "id": "unknown",
                    "kind": "mystery",
                    "title": "unknown provider item",
                    "body": "must not render",
                    "metadata": {},
                    "created_at": "2026-09-14T12:00:00Z",
                }
            ]
        },
    )

    assert brief.build_brief("user-a")["cards"] == []


def test_native_babyg_renders_lowercase_and_native_dm_noise_is_excluded(monkeypatch) -> None:
    _connections(monkeypatch)
    _stub_rows(
        monkeypatch,
        notifications={
            "user-a": [
                {
                    "id": "native-request",
                    "kind": "connection_request",
                    "title": "Creator sent a connection request",
                    "body": "review the real request",
                    "metadata": {},
                    "created_at": "2026-09-14T12:00:00Z",
                },
                {
                    "id": "native-dm",
                    "kind": "new_dm",
                    "title": "native dm should stay out",
                    "body": "ordinary inbox message",
                    "metadata": {},
                    "created_at": "2026-09-14T12:00:00Z",
                },
            ]
        },
    )

    view = brief.build_brief("user-a")

    assert len(view["cards"]) == 1
    assert view["cards"][0]["platform"] == "babyg"
    assert view["cards"][0]["platform_label"] == "babyg"


def test_instagram_send_proposal_is_not_surfaced(monkeypatch) -> None:
    _connections(monkeypatch, instagram=True)
    _stub_rows(
        monkeypatch,
        proposals={
            "user-a": [
                {
                    "id": "ig-send",
                    "user_id": "user-a",
                    "action_type": "instagram.send_dm",
                    "provider": "instagram",
                    "preview": {"title": "send instagram reply"},
                    "created_at": "2026-09-14T12:00:00Z",
                }
            ]
        },
    )

    assert brief.build_brief("user-a")["cards"] == []


def test_home_preview_uses_same_real_matter_source(monkeypatch) -> None:
    _connections(monkeypatch, gmail=True, instagram=True)
    _stub_rows(
        monkeypatch,
        proposals={"user-a": [_proposal("user-a", title="draft reply to Alpha")]},
        notifications={"user-a": [_instagram_notification("user-a", title="@alpha rates")]},
    )

    cards = brief.build_brief("user-a")["cards"]
    rows = brief.home_preview_rows("user-a")

    assert [row["title"] for row in rows] == [card["headline"] for card in cards[:3]]
    assert all(row["href"] == "/creator/brief" for row in rows)
