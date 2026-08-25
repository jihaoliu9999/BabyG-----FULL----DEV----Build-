"""Unified Discover route and service contract tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.main import app
from app.services import discover as discover_service
from app.services import network, notifications, profiles


def _card(kind: str = "opportunity", **overrides: Any) -> dict[str, Any]:
    card_id = str(uuid4())
    owner_id = str(uuid4())
    detail_prefix = {
        "creator": "/creator/network",
        "brand": "/creator/discover/brand",
        "opportunity": "/creator/jobs",
    }[kind]
    card = {
        "card_kind": kind,
        "card_id": card_id,
        "owner_user_id": owner_id,
        "title": "Summer campaign" if kind == "opportunity" else "Atelier Fig",
        "subtitle": "verified brand" if kind == "brand" else "fashion · New York",
        "image_url": None,
        "location_label": "New York, NY",
        "tags": ["fashion", "lifestyle"],
        "created_at": "2026-06-17T12:00:00Z",
        "description": "Create two short-form videos for a summer launch.",
        "profile_handle": None,
        "follower_range": None,
        "primary_platform": None,
        "verification_status": "verified" if kind == "brand" else None,
        "compensation_type": "flat_rate" if kind == "opportunity" else None,
        "compensation_text": "$750 flat rate" if kind == "opportunity" else None,
        "budget_min": 750 if kind == "opportunity" else None,
        "budget_max": 750 if kind == "opportunity" else None,
        "deadline": "2026-07-01T00:00:00Z" if kind == "opportunity" else None,
        "detail_path": f"{detail_prefix}/{card_id}",
        "why_relevant": "matches your fashion focus",
    }
    card.update(overrides)
    return card


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, follow_redirects=False)


def _signed_in(client: TestClient, *, role: str = "creator", user_id: str | None = None) -> str:
    uid = user_id or str(uuid4())
    response = Response()
    write_session(response, {"user_id": uid, "role": role})
    cookie = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)
    return uid


@pytest.fixture()
def discover_world(monkeypatch):
    cards = [_card("opportunity"), _card("brand"), _card("creator")]
    actions: list[dict[str, Any]] = []
    connections: list[tuple[str, str]] = []
    sent_notifications: list[dict[str, Any]] = []

    monkeypatch.setattr(
        profiles,
        "get_creator_profile",
        lambda uid: {"user_id": uid, "full_name": "Alex", "niches": ["fashion"]},
    )
    monkeypatch.setattr(
        discover_service,
        "list_cards",
        lambda **kwargs: cards,
    )
    monkeypatch.setattr(discover_service, "last_undoable_pass", lambda uid: None)
    monkeypatch.setattr(
        discover_service,
        "get_card",
        lambda *, card_kind, card_id: next(
            (
                card
                for card in cards
                if card["card_kind"] == card_kind and card["card_id"] == card_id
            ),
            None,
        ),
    )

    def _record_action(**kwargs):
        actions.append(kwargs)
        return True

    def _request_connection(*, requester_id, addressee_id):
        connections.append((requester_id, addressee_id))
        return True

    def _notify(**kwargs):
        sent_notifications.append(kwargs)
        return True

    monkeypatch.setattr(discover_service, "record_action", _record_action)
    monkeypatch.setattr(network, "request_connection", _request_connection)
    monkeypatch.setattr(notifications, "create", _notify)
    return {
        "cards": cards,
        "actions": actions,
        "connections": connections,
        "notifications": sent_notifications,
    }


def test_discover_renders_mobile_first_mixed_stack(client, discover_world):
    _signed_in(client)
    response = client.get("/creator/discover")
    assert response.status_code == 200
    assert "Summer campaign" in response.text
    assert "Atelier Fig" in response.text
    assert 'data-action-dock' in response.text
    assert 'data-card-kind="opportunity"' in response.text
    assert "discover.js" in response.text
    assert discover_world["actions"][0]["action_type"] == "viewed"


def test_discover_filters_are_forwarded(client, discover_world, monkeypatch):
    seen: dict[str, Any] = {}

    def _list_cards(**kwargs):
        seen.update(kwargs)
        return discover_world["cards"]

    monkeypatch.setattr(discover_service, "list_cards", _list_cards)
    _signed_in(client)
    response = client.get(
        "/creator/discover?kind=opportunity&category=fashion&location=brooklyn"
        "&budget_min=500&budget_max=2000"
    )
    assert response.status_code == 200
    assert seen["kind"] == "opportunity"
    assert seen["category"] == "fashion"
    assert seen["location"] == "brooklyn"
    assert seen["budget_min"] == 500
    assert seen["budget_max"] == 2000


def test_discover_requires_creator_role(client, discover_world):
    _signed_in(client, role="operator")
    assert client.get("/creator/discover").status_code == 403


def test_pass_records_kind_and_card_id(client, discover_world):
    viewer_id = _signed_in(client)
    card = discover_world["cards"][0]
    response = client.post(
        "/creator/discover/swipe",
        data={
            "target_kind": card["card_kind"],
            "target_card_id": card["card_id"],
            "action": "passed",
            "kind": "all",
        },
    )
    assert response.status_code == 303
    action = discover_world["actions"][-1]
    assert action["user_id"] == viewer_id
    assert action["target_kind"] == "opportunity"
    assert action["target_card_id"] == card["card_id"]
    assert action["action_type"] == "passed"
    assert discover_world["connections"] == []


def test_swipe_redirect_preserves_budget_filters(client, discover_world):
    _signed_in(client)
    card = discover_world["cards"][0]
    response = client.post(
        "/creator/discover/swipe",
        data={
            "target_kind": card["card_kind"],
            "target_card_id": card["card_id"],
            "action": "passed",
            "kind": "opportunity",
            "budget_min": "500",
            "budget_max": "2000",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == (
        "/creator/discover?kind=opportunity&budget_min=500&budget_max=2000"
    )


def test_opportunity_interest_requests_connection(client, discover_world):
    viewer_id = _signed_in(client)
    card = discover_world["cards"][0]
    response = client.post(
        "/creator/discover/swipe",
        data={
            "target_kind": "opportunity",
            "target_card_id": card["card_id"],
            "action": "interested",
        },
    )
    assert response.status_code == 303
    assert discover_world["connections"] == [(viewer_id, card["owner_user_id"])]
    assert discover_world["notifications"][0]["kind"] == "connection_request"


def test_mismatched_primary_action_is_rejected(client, discover_world):
    _signed_in(client)
    card = discover_world["cards"][0]
    response = client.post(
        "/creator/discover/swipe",
        data={
            "target_kind": "opportunity",
            "target_card_id": card["card_id"],
            "action": "connected",
        },
    )
    assert response.status_code == 400


def test_view_details_redirects_without_connection(client, discover_world):
    _signed_in(client)
    card = discover_world["cards"][1]
    response = client.post(
        "/creator/discover/swipe",
        data={
            "target_kind": "brand",
            "target_card_id": card["card_id"],
            "action": "opened_profile",
        },
    )
    assert response.status_code == 303
    assert response.headers["location"] == card["detail_path"]
    assert discover_world["connections"] == []


def test_brand_detail_route_renders_discover_safe_card(client, discover_world):
    _signed_in(client)
    card = discover_world["cards"][1]
    response = client.get(card["detail_path"])
    assert response.status_code == 200
    assert "Atelier Fig" in response.text
    assert "say hi" in response.text


def test_clean_kind_and_relevance_are_closed_vocab():
    assert discover_service.clean_kind("brand") == "brand"
    assert discover_service.clean_kind("nonsense") == "all"
    assert (
        discover_service._why_relevant(["Fashion", "Food"], ["fashion"], "creator")
        == "matches your fashion focus"
    )


def test_normalize_rejects_invalid_ids():
    raw = _card("creator")
    raw["card_id"] = "not-a-uuid"
    assert discover_service._normalize_card(raw, viewer_tags=[]) is None


# ---------------------------------------------------------------------------
# Query-bounding tests — patch 2A
#
# The v1 implementation always fetched HARD_LIMIT*4 = 120 rows regardless
# of what the caller asked for, and read the entire per-user action
# history to compute exclusions. These tests lock in the new behavior so
# a home-preview render (limit=3) no longer over-fetches, and so the
# exclusion query is bounded with an order + hard cap.
# ---------------------------------------------------------------------------


class _RecordingTable:
    """Chainable Supabase-table stub that records calls verbatim.

    Enough of the supabase-py builder surface to be a drop-in for
    `.select().eq().neq().in_().order().limit().contains().ilike()
    .gte().lte().execute()`.
    """

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = list(rows)
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def _record(self, name: str, *args: Any, **kwargs: Any) -> _RecordingTable:
        self.calls.append((name, args, kwargs))
        return self

    def select(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("select", *a, **k)

    def neq(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("neq", *a, **k)

    def eq(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("eq", *a, **k)

    def in_(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("in_", *a, **k)

    def order(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("order", *a, **k)

    def limit(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("limit", *a, **k)

    def contains(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("contains", *a, **k)

    def ilike(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("ilike", *a, **k)

    def gte(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("gte", *a, **k)

    def lte(self, *a: Any, **k: Any) -> _RecordingTable:
        return self._record("lte", *a, **k)

    def execute(self):
        from types import SimpleNamespace

        return SimpleNamespace(data=list(self._rows))


class _RecordingClient:
    def __init__(self, tables: dict[str, _RecordingTable]) -> None:
        self._tables = tables

    def table(self, name: str) -> _RecordingTable:
        return self._tables[name]


def _viewer_uid() -> str:
    return str(uuid4())


def _fetch_limit_used(cards_table: _RecordingTable) -> int:
    """Last `.limit(...)` value applied to the discovery_cards query."""
    limits = [args[0] for name, args, _ in cards_table.calls if name == "limit"]
    assert limits, "list_cards did not call .limit() on discovery_cards"
    return int(limits[-1])


def test_list_cards_home_preview_does_not_overfetch_120_rows(monkeypatch):
    """Home preview asks for 3 cards → we must NOT read the old 120."""
    cards_table = _RecordingTable(rows=[])
    actions_table = _RecordingTable(rows=[])
    monkeypatch.setattr(
        discover_service.supabase_client,
        "get_service_client",
        lambda: _RecordingClient(
            {
                "discovery_cards": cards_table,
                "creator_discovery_actions": actions_table,
            }
        ),
    )
    discover_service.list_cards(
        viewer_id=_viewer_uid(),
        viewer_role="creator",
        kind="all",
        limit=3,
    )
    used = _fetch_limit_used(cards_table)
    # Old behavior was 120 unconditionally. New behavior scales to a
    # small exclusion buffer above the requested slice.
    assert used < 120
    assert used >= 3  # room for at least the requested slice
    assert used <= 30  # tiny caller stays tiny


def test_list_cards_default_slice_stays_below_old_ceiling(monkeypatch):
    """A default /creator/discover render (limit=DEFAULT_LIMIT=12) also
    no longer fetches 120."""
    cards_table = _RecordingTable(rows=[])
    actions_table = _RecordingTable(rows=[])
    monkeypatch.setattr(
        discover_service.supabase_client,
        "get_service_client",
        lambda: _RecordingClient(
            {
                "discovery_cards": cards_table,
                "creator_discovery_actions": actions_table,
            }
        ),
    )
    discover_service.list_cards(
        viewer_id=_viewer_uid(),
        viewer_role="creator",
        kind="all",
    )
    used = _fetch_limit_used(cards_table)
    assert used < 120
    assert used >= discover_service.DEFAULT_LIMIT


def test_list_cards_max_caller_stays_capped(monkeypatch):
    """A caller asking at HARD_LIMIT gets a bounded buffer, never above
    the historical HARD_LIMIT*4 ceiling."""
    cards_table = _RecordingTable(rows=[])
    actions_table = _RecordingTable(rows=[])
    monkeypatch.setattr(
        discover_service.supabase_client,
        "get_service_client",
        lambda: _RecordingClient(
            {
                "discovery_cards": cards_table,
                "creator_discovery_actions": actions_table,
            }
        ),
    )
    discover_service.list_cards(
        viewer_id=_viewer_uid(),
        viewer_role="creator",
        kind="all",
        limit=discover_service.HARD_LIMIT,
    )
    used = _fetch_limit_used(cards_table)
    assert used <= discover_service.HARD_LIMIT * 4


def test_excluded_card_keys_query_is_ordered_and_capped(monkeypatch):
    """The exclusion history read must include an order + hard limit so
    a power user with thousands of actions can't force a full-table
    scan on every discover render."""
    actions_table = _RecordingTable(rows=[])
    monkeypatch.setattr(
        discover_service.supabase_client,
        "get_service_client",
        lambda: _RecordingClient({"creator_discovery_actions": actions_table}),
    )
    discover_service._excluded_card_keys("00000000-0000-0000-0000-000000000001")

    call_names = [name for name, _, _ in actions_table.calls]
    assert "order" in call_names, "exclusion query must be ordered"
    assert "limit" in call_names, "exclusion query must be bounded"

    order_args = next(args for name, args, _ in actions_table.calls if name == "order")
    order_kwargs = next(kw for name, _, kw in actions_table.calls if name == "order")
    assert order_args[0] == "created_at"
    assert order_kwargs.get("desc") is True

    limit_arg = next(args[0] for name, args, _ in actions_table.calls if name == "limit")
    assert isinstance(limit_arg, int) and 0 < limit_arg <= 1000


def test_excluded_card_keys_still_honors_passed_cooldown_and_commits(monkeypatch):
    """The bounded read must not change exclusion semantics: recent
    passes still exclude, older passes past the 30-day cooldown do
    not, commits (saved/connected/interested) are permanent, and an
    undo after a pass restores the card."""
    kind = "creator"
    committed = str(uuid4())
    recently_passed = str(uuid4())
    old_passed = str(uuid4())
    passed_then_undone = str(uuid4())

    now = datetime.now(UTC)
    yesterday = (now - timedelta(days=1)).isoformat()
    thirty_five_days_ago = (now - timedelta(days=35)).isoformat()
    two_days_ago = (now - timedelta(days=2)).isoformat()

    rows = [
        {
            "target_kind": kind,
            "target_card_id": committed,
            "action_type": "saved",
            "created_at": (now - timedelta(days=100)).isoformat(),
        },
        {
            "target_kind": kind,
            "target_card_id": recently_passed,
            "action_type": "passed",
            "created_at": yesterday,
        },
        {
            "target_kind": kind,
            "target_card_id": old_passed,
            "action_type": "passed",
            "created_at": thirty_five_days_ago,
        },
        {
            "target_kind": kind,
            "target_card_id": passed_then_undone,
            "action_type": "passed",
            "created_at": two_days_ago,
        },
        {
            "target_kind": kind,
            "target_card_id": passed_then_undone,
            "action_type": "undo_pass",
            "created_at": yesterday,
        },
    ]

    actions_table = _RecordingTable(rows=rows)
    monkeypatch.setattr(
        discover_service.supabase_client,
        "get_service_client",
        lambda: _RecordingClient({"creator_discovery_actions": actions_table}),
    )

    excluded = discover_service._excluded_card_keys(
        "00000000-0000-0000-0000-000000000002"
    )
    assert (kind, committed) in excluded
    assert (kind, recently_passed) in excluded
    assert (kind, old_passed) not in excluded  # past cooldown
    assert (kind, passed_then_undone) not in excluded  # undo wins
