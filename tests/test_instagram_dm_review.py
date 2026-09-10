from __future__ import annotations

from typing import Any
from uuid import uuid4

from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.integrations.anthropic_client import ClaudeCallError, ClaudeResponse
from app.main import app
from app.routes import creator as creator_routes
from app.services import instagram_dms


class _Result:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, rows):
        self.rows = rows
        self.filters: dict[str, Any] = {}
        self._limit: int | None = None

    def select(self, *_a, **_kw):
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def gt(self, *_a, **_kw):
        return self

    def order(self, *_a, **_kw):
        return self

    def limit(self, value, **_kw):
        self._limit = int(value)
        return self

    def insert(self, payload):
        self.rows.append(payload)
        return self

    def execute(self):
        rows = [
            row
            for row in self.rows
            if all(str(row.get(key)) == str(value) for key, value in self.filters.items())
        ]
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)


class _Client:
    def __init__(self, tables):
        self.tables = tables

    def table(self, name):
        return _Query(self.tables.setdefault(name, []))


def _signed_in(client: TestClient, *, user_id: str = "user-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    cookie = resp.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    client.cookies.set(SESSION_COOKIE, cookie)


def test_sender_label_does_not_render_numeric_id_as_username():
    assert (
        instagram_dms.sender_label(
            {"peer_username": "12345", "ig_peer_user_id": "987654321"}
        )
        == "instagram user 4321"
    )
    assert instagram_dms.sender_label({"peer_username": "real_creator"}) == "@real_creator"


def test_instagram_evaluation_checks_thread_ownership(monkeypatch):
    thread_id = str(uuid4())
    tables = {
        "instagram_dm_threads": [
            {
                "id": thread_id,
                "creator_id": "user-1",
                "ig_thread_id": "ig-thread-1",
                "ig_peer_user_id": "987654321",
            }
        ]
    }
    monkeypatch.setattr(
        instagram_dms.supabase_client,
        "get_service_client",
        lambda: _Client(tables),
    )

    assert instagram_dms._get_thread_for_creator("user-1", thread_id) is not None
    assert instagram_dms._get_thread_for_creator("user-2", thread_id) is None


def test_instagram_evaluator_uses_persisted_message_text(monkeypatch):
    thread_id = str(uuid4())
    message_id = str(uuid4())
    captured: dict[str, Any] = {}
    tables = {
        "instagram_dm_threads": [
            {
                "id": thread_id,
                "creator_id": "user-1",
                "ig_thread_id": "ig-thread-1",
                "ig_peer_user_id": "987654321",
                "peer_username": None,
                "unread_count": 1,
            }
        ],
        "instagram_dm_messages": [
            {
                "id": message_id,
                "thread_id": thread_id,
                "creator_id": "user-1",
                "direction": "inbound",
                "sender_ig_id": "987654321",
                "body": "Hi, I want to talk about a potential creator partnership.",
                "attachments": [],
                "received_at": "2026-09-10T01:00:00Z",
            }
        ],
        "instagram_dm_evaluations": [],
    }
    monkeypatch.setattr(
        instagram_dms.supabase_client,
        "get_service_client",
        lambda: _Client(tables),
    )

    def _complete(**kwargs):
        captured["message"] = kwargs["messages"][0]["content"]
        return ClaudeResponse(
            text='{"summary":"partnership inquiry","worth_responding":"yes","why":"business signal","opportunity":"creator partnership","risk":"unverified sender","urgency":"not urgent","missing_information":"budget","suggested_next_steps":"verify sender"}'
        )

    monkeypatch.setattr(instagram_dms.anthropic_client, "complete_chat", _complete)

    review = instagram_dms.evaluate_thread_for_creator("user-1", thread_id)

    assert review["state"] == "success"
    assert "potential creator partnership" in captured["message"]
    assert review["evaluation"]["Worth responding?"] == "yes"
    assert "creator partnership" in review["evaluation"]["Opportunity"]
    assert tables["instagram_dm_evaluations"][0]["message_id"] == message_id


def test_instagram_provider_failure_state(monkeypatch):
    thread_id = str(uuid4())
    tables = {
        "instagram_dm_threads": [
            {
                "id": thread_id,
                "creator_id": "user-1",
                "ig_thread_id": "ig-thread-1",
                "ig_peer_user_id": "987654321",
            }
        ],
        "instagram_dm_messages": [
            {
                "id": str(uuid4()),
                "thread_id": thread_id,
                "creator_id": "user-1",
                "direction": "inbound",
                "body": "brand deal?",
                "attachments": [],
            }
        ],
    }
    monkeypatch.setattr(
        instagram_dms.supabase_client,
        "get_service_client",
        lambda: _Client(tables),
    )

    def _boom(**_kwargs):
        raise ClaudeCallError("down")

    monkeypatch.setattr(instagram_dms.anthropic_client, "complete_chat", _boom)

    assert (
        instagram_dms.evaluate_thread_for_creator("user-1", thread_id)["state"]
        == "provider_failure"
    )


def test_instagram_review_route_has_one_evaluate_button(monkeypatch):
    client = TestClient(app, follow_redirects=False)
    _signed_in(client)
    thread_id = str(uuid4())
    message_id = str(uuid4())
    thread = {
        "id": thread_id,
        "ig_thread_id": "ig-thread-1",
        "ig_peer_user_id": "987654321",
        "peer_username": "creator",
        "unread_count": 1,
        "last_message_at": "2026-09-10T01:00:00Z",
    }
    message = {
        "id": message_id,
        "thread_id": thread_id,
        "direction": "inbound",
        "body": "Potential UGC deal",
        "received_at": "2026-09-10T01:00:00Z",
        "attachments": [],
    }
    monkeypatch.setattr(
        creator_routes.profiles,
        "get_creator_profile",
        lambda *_a: {"onboarding_completed_at": "2026-01-01T00:00:00Z"},
    )
    monkeypatch.setattr(
        creator_routes.instagram_dms,
        "list_threads_for_creator",
        lambda *_a, **_kw: [thread],
    )
    monkeypatch.setattr(
        creator_routes.instagram_dms,
        "list_messages_for_thread",
        lambda *_a, **_kw: [message],
    )
    monkeypatch.setattr(
        creator_routes.instagram_dms,
        "manager_review_for_thread",
        lambda *_a, **_kw: {
            "counterparty": "@creator",
            "read": "possible business inquiry",
            "why": "mentions UGC",
            "next_step": "Verify scope, usage rights, timeline, and budget.",
            "business_signal": True,
            "attachment_label": None,
            "attachment_types": [],
            "message_count": 1,
            "hidden_count": 0,
            "visible_messages": [message],
            "latest_received_at": "2026-09-10T01:00:00Z",
            "latest_preview": "Potential UGC deal",
        },
    )
    monkeypatch.setattr(
        creator_routes.instagram_dms,
        "latest_evaluation_for_thread",
        lambda *_a, **_kw: None,
    )
    monkeypatch.setattr(
        creator_routes.instagram_dms,
        "mark_thread_read_for_creator",
        lambda **_kw: True,
    )
    monkeypatch.setattr(
        creator_routes.notifications,
        "mark_thread_read",
        lambda **_kw: True,
    )

    response = client.get(f"/creator/instagram/dms?thread={thread_id}")

    assert response.status_code == 200
    assert response.text.count("Evaluate this message") == 1
    assert "draft reply" not in response.text
    assert "ask BabyG" not in response.text
    assert "ask babyg" not in response.text
    assert "Potential UGC deal" in response.text
