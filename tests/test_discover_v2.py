"""Unified Discover route and service contract tests."""

from __future__ import annotations

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
