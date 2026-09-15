"""Tests for the Instagram DM ingestion service."""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from app.integrations.anthropic_client import ClaudeNotConfiguredError, ClaudeResponse
from app.services import brief as brief_service
from app.services import instagram_dms


class _FakeSupabase:
    def __init__(self):
        # Fake tables. oauth_connections is a static list; the two
        # instagram_dm tables are lists that get appended to on
        # inserts.
        self.oauth_rows: list[dict] = []
        self.threads: list[dict] = []
        self.messages: list[dict] = []
        self.evaluations: list[dict] = []
        self.notifications: list[dict] = []
        self._table_id_seq = 0
        self.raise_on: set[str] = set()

    def next_id(self) -> str:
        self._table_id_seq += 1
        return f"row-{self._table_id_seq}"

    def table(self, name):
        if name in self.raise_on:
            class _Boom:
                def __getattr__(self, _):
                    raise RuntimeError(f"supabase down ({name})")
            return _Boom()
        return _FakeTable(self, name)


class _FakeTable:
    def __init__(self, store, name):
        self.store = store
        self.name = name
        self._filter: dict = {}
        self._gt_filter: dict = {}
        self._upsert_row = None
        self._insert_row = None
        self._update_payload = None

    def select(self, _cols, **_kwargs):
        return self

    def eq(self, col, val):
        self._filter[col] = val
        return self

    def gt(self, col, val):
        self._gt_filter[col] = val
        return self

    def is_(self, col, val):
        self._filter[col] = None if val == "null" else val
        return self

    def in_(self, col, vals):
        self._filter[col] = list(vals)
        return self

    def order(self, *_, **__):
        return self

    def limit(self, _):
        return self

    def upsert(self, row, on_conflict=None, **_kwargs):
        self._upsert_row = (row, on_conflict)
        return self

    def insert(self, row):
        self._insert_row = row
        return self

    def update(self, payload):
        self._update_payload = payload
        return self

    def _matches(self, row: dict) -> bool:
        for key, value in self._filter.items():
            if isinstance(value, list):
                if row.get(key) not in value:
                    return False
            elif row.get(key) != value:
                return False
        return True

    def execute(self):
        # Handle the filter-first paths (select or update)
        if self.name == "oauth_connections":
            rows = [
                r for r in self.store.oauth_rows
                if self._matches(r)
            ]
            return _Result(rows)
        if self.name == "instagram_dm_threads":
            if self._insert_row is not None:
                row = {**self._insert_row, "id": self.store.next_id()}
                self.store.threads.append(row)
                return _Result([row])
            if self._update_payload is not None:
                for r in self.store.threads:
                    if r["id"] == self._filter.get("id"):
                        r.update(self._update_payload)
                        return _Result([r])
                return _Result([])
            # select
            rows = [
                r for r in self.store.threads
                if self._matches(r)
                and all(
                    (r.get(k) or 0) > v for k, v in self._gt_filter.items()
                )
            ]
            return _Result(rows)
        if self.name == "instagram_dm_messages":
            if self._upsert_row is not None:
                row, _ = self._upsert_row
                already = next(
                    (
                        m
                        for m in self.store.messages
                        if m.get("creator_id") == row.get("creator_id")
                        and m.get("ig_message_id") == row.get("ig_message_id")
                    ),
                    None,
                )
                if already:
                    return _Result([already])
                stored = {**row, "id": self.store.next_id()}
                self.store.messages.append(stored)
                return _Result([stored])
            rows = [
                r for r in self.store.messages
                if self._matches(r)
            ]
            return _Result(rows)
        if self.name == "notifications":
            if self._upsert_row is not None:
                row, _ = self._upsert_row
                already = next(
                    (
                        n
                        for n in self.store.notifications
                        if n.get("user_id") == row.get("user_id")
                        and n.get("source_provider") == row.get("source_provider")
                        and n.get("source_event_id") == row.get("source_event_id")
                    ),
                    None,
                )
                if already:
                    return _Result([already])
                stored = {**row, "id": self.store.next_id()}
                self.store.notifications.append(stored)
                return _Result([stored])
            if self._insert_row is not None:
                stored = {**self._insert_row, "id": self.store.next_id()}
                self.store.notifications.append(stored)
                return _Result([stored])
        if self.name == "instagram_dm_evaluations":
            if self._insert_row is not None:
                stored = {**self._insert_row, "id": self.store.next_id()}
                self.store.evaluations.append(stored)
                return _Result([stored])
            rows = [
                r for r in self.store.evaluations
                if self._matches(r)
            ]
            return _Result(rows)
        return _Result([])


class _Result:
    def __init__(self, data):
        self.data = data


def _install(monkeypatch) -> _FakeSupabase:
    fake = _FakeSupabase()
    monkeypatch.setattr(
        instagram_dms.supabase_client, "get_service_client", lambda: fake
    )
    return fake


@pytest.fixture(autouse=True)
def _no_real_claude(monkeypatch) -> None:
    def _not_configured(**_kwargs):
        raise ClaudeNotConfiguredError("test default: no real Claude calls")

    monkeypatch.setattr(instagram_dms.anthropic_client, "complete_chat", _not_configured)


def _mock_claude_evaluation(monkeypatch, *, worth: str, summary: str) -> list[str]:
    prompts: list[str] = []

    def _complete(**kwargs):
        prompts.append(str(kwargs["messages"][0]["content"]))
        return ClaudeResponse(
            text=json.dumps({
                "summary": summary,
                "worth_responding": worth,
                "why": "stored Instagram evidence was evaluated",
                "opportunity": (
                    "paid campaign opportunity" if worth.lower() == "yes" else "none"
                ),
                "risk": "scope unknown",
                "urgency": "not urgent",
                "missing_information": "usage rights and timeline",
                "suggested_next_steps": "ask for campaign scope before quoting",
            })
        )

    monkeypatch.setattr(instagram_dms.anthropic_client, "complete_chat", _complete)
    return prompts


def _connect_brief_to_fake(monkeypatch, fake: _FakeSupabase, *, instagram: bool = True) -> None:
    monkeypatch.setattr(brief_service.supabase_client, "get_service_client", lambda: fake)
    monkeypatch.setattr(brief_service.action_proposals, "list_pending_for_user", lambda **_kw: [])
    monkeypatch.setattr(brief_service.notifications, "list_for_user", lambda *_a, **_kw: [])
    monkeypatch.setattr(brief_service.oauth_connections, "get_google_connection", lambda _user_id: None)
    monkeypatch.setattr(
        brief_service.oauth_connections,
        "get_instagram_connection",
        lambda user_id: {
            "user_id": user_id,
            "access_token": "present",
            "provider_account_id": "acct-1",
        }
        if instagram
        else None,
    )
    monkeypatch.setattr(brief_service.oauth_connections, "google_gmail_connected", lambda _c: False)
    monkeypatch.setattr(brief_service.oauth_connections, "google_gmail_compose_connected", lambda _c: False)
    monkeypatch.setattr(brief_service.oauth_connections, "google_gmail_send_connected", lambda _c: False)
    monkeypatch.setattr(brief_service.oauth_connections, "google_calendar_connected", lambda _c: False)


def _payload(
    *,
    account_id: str = "acct-1",
    peer_id: str = "peer-99",
    mid: str,
    text: str | None = None,
    username: str | None = "brandco",
    is_echo: bool = False,
    attachments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"mid": mid}
    if text is not None:
        message["text"] = text
    if is_echo:
        message["is_echo"] = True
    if attachments is not None:
        message["attachments"] = attachments
    sender = {"id": account_id if is_echo else peer_id}
    recipient = {"id": peer_id if is_echo else account_id}
    if username and not is_echo:
        sender["username"] = username
    return {
        "object": "instagram",
        "entry": [{
            "id": account_id,
            "messaging": [{
                "sender": sender,
                "recipient": recipient,
                "timestamp": 1699999999000,
                "message": message,
            }],
        }],
    }


# ---- top-level guards ------------------------------------------------


def test_ingest_ignores_non_dict_payload(monkeypatch) -> None:
    _install(monkeypatch)
    assert instagram_dms.ingest_webhook_payload(None) == {  # type: ignore[arg-type]
        "entries": 0,
        "messages_ingested": 0,
        "dropped_no_creator": 0,
        "errors": 0,
    }


def test_ingest_ignores_non_instagram_object(monkeypatch) -> None:
    _install(monkeypatch)
    stats = instagram_dms.ingest_webhook_payload(
        {"object": "page", "entry": [{"id": "x", "messaging": []}]}
    )
    assert stats["entries"] == 0


def test_manager_review_handles_attachment_only_reel_context() -> None:
    review = instagram_dms.manager_review_for_thread(
        {"id": "thread-1", "peer_username": "brandco"},
        [
            {
                "direction": "inbound",
                "body": None,
                "attachments": [{"type": "reel", "payload": {"id": "r1"}}],
                "received_at": "2026-09-08T10:00:00Z",
            }
        ],
    )

    assert review["counterparty"] == "@brandco"
    assert review["read"] == "instagram reel needs review"
    assert review["attachment_types"] == ["reel"]
    assert "shared media" in review["next_step"]


def test_manager_review_recommends_terms_for_brandish_dm() -> None:
    review = instagram_dms.manager_review_for_thread(
        {"id": "thread-1", "ig_peer_user_id": "peer-1"},
        [
            {
                "direction": "inbound",
                "body": "Can we do a paid collab next month?",
                "attachments": [],
                "received_at": "2026-09-08T10:00:00Z",
            }
        ],
    )

    assert review["business_signal"] is True
    assert review["read"] == "possible business inquiry"
    assert "usage rights" in review["next_step"]


# ---- unknown IG account ----------------------------------------------


def test_ingest_drops_entry_when_no_creator_matches(monkeypatch, caplog) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = []  # nobody has connected IG account "999"
    with caplog.at_level(logging.INFO):
        stats = instagram_dms.ingest_webhook_payload({
            "object": "instagram",
            "entry": [{
                "id": "999",
                "messaging": [{
                    "sender": {"id": "peer1"},
                    "recipient": {"id": "999"},
                    "timestamp": 1699999999000,
                    "message": {"mid": "m1", "text": "hi"},
                }],
            }],
        })
    assert stats == {
        "entries": 1,
        "messages_ingested": 0,
        "dropped_no_creator": 1,
        "errors": 0,
    }
    assert fake.messages == []
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "instagram_dms.resolve_creator.not_found" in log_text
    assert "instagram_dms.dropped.no_creator" in log_text


# ---- happy path: inbound message -------------------------------------


def test_ingest_persists_inbound_message_and_creates_thread(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {"mid": "m1", "text": "love your reel"},
            }],
        }],
    })
    assert stats["messages_ingested"] == 1
    assert stats["errors"] == 0
    # Thread created
    assert len(fake.threads) == 1
    thread = fake.threads[0]
    assert thread["creator_id"] == "creator-1"
    assert thread["ig_thread_id"] == "peer-99"
    assert thread["unread_count"] == 1
    # Message stored
    assert len(fake.messages) == 1
    m = fake.messages[0]
    assert m["direction"] == "inbound"
    assert m["body"] == "love your reel"
    assert fake.notifications == []


def test_ingest_resolves_actual_instagram_login_webhook_user_id_shape(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {
            "user_id": "creator-1",
            "provider": "instagram",
            "provider_account_id": "17841440333695396",
        }
    ]

    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "17841440333695396",
            "messaging": [{
                "sender": {"id": "812345678901234"},
                "recipient": {"id": "28475339705441642"},
                "timestamp": 1699999999000,
                "message": {"mid": "m-actual-shape", "text": "paid collab rates?"},
            }],
        }],
    })

    assert stats["messages_ingested"] == 1
    assert stats["dropped_no_creator"] == 0
    assert fake.threads[0]["creator_id"] == "creator-1"
    assert fake.threads[0]["ig_thread_id"] == "812345678901234"
    assert fake.messages[0]["ig_message_id"] == "m-actual-shape"
    assert fake.messages[0]["direction"] == "inbound"
    assert len(fake.notifications) == 1


def test_new_inbound_business_dm_auto_evaluates_and_brief_consumes(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    prompts = _mock_claude_evaluation(
        monkeypatch,
        worth="yes",
        summary="brand wants rates for a paid reels campaign",
    )

    stats = instagram_dms.ingest_webhook_payload(
        _payload(
            mid="m-business",
            text=(
                "We're interested in a paid campaign. Looking for 2 reels. "
                "Can you send your rates?"
            ),
        )
    )

    assert stats["messages_ingested"] == 1
    assert len(fake.messages) == 1
    assert len(prompts) == 1
    assert "paid campaign" in prompts[0]
    assert len(fake.evaluations) == 1
    evaluation = fake.evaluations[0]
    assert evaluation["creator_id"] == "creator-1"
    assert evaluation["thread_id"] == fake.threads[0]["id"]
    assert evaluation["message_id"] == fake.messages[0]["id"]
    assert evaluation["result"]["Worth responding?"] == "yes"

    _connect_brief_to_fake(monkeypatch, fake)
    cards = brief_service.build_brief("creator-1")["cards"]
    assert any(
        card["platform"] == "instagram"
        and "paid reels campaign" in card["headline"]
        for card in cards
    )


def test_noise_inbound_dm_auto_evaluates_but_brief_does_not_surface(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    prompts = _mock_claude_evaluation(
        monkeypatch,
        worth="no",
        summary="casual compliment",
    )

    stats = instagram_dms.ingest_webhook_payload(
        _payload(mid="m-noise", text="love your content")
    )

    assert stats["messages_ingested"] == 1
    assert len(prompts) == 1
    assert len(fake.evaluations) == 1
    assert fake.evaluations[0]["result"]["Worth responding?"] == "no"
    assert fake.notifications == []

    _connect_brief_to_fake(monkeypatch, fake)
    assert brief_service.build_brief("creator-1")["cards"] == []


def test_ingest_does_not_route_by_messaging_recipient_id(monkeypatch, caplog) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {
            "user_id": "creator-1",
            "provider": "instagram",
            "provider_account_id": "28475339705441642",
        }
    ]

    with caplog.at_level(logging.INFO):
        stats = instagram_dms.ingest_webhook_payload({
            "object": "instagram",
            "entry": [{
                "id": "17841440333695396",
                "messaging": [{
                    "sender": {"id": "812345678901234"},
                    "recipient": {"id": "28475339705441642"},
                    "timestamp": 1699999999000,
                    "message": {"mid": "m-no-recipient-fallback", "text": "hi"},
                }],
            }],
        })

    assert stats["messages_ingested"] == 0
    assert stats["dropped_no_creator"] == 1
    assert fake.messages == []
    assert fake.notifications == []
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "instagram_dms.resolve_creator.not_found" in log_text


def test_multi_user_event_auto_evaluates_only_resolved_creator(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-a", "provider": "instagram", "provider_account_id": "acct-a"},
        {"user_id": "creator-b", "provider": "instagram", "provider_account_id": "acct-b"},
    ]
    calls: list[tuple[str, str]] = []

    def _evaluate(user_id: str, thread_id: str):
        calls.append((user_id, thread_id))
        return {"ok": True, "state": "success", "evaluation": {"Worth responding?": "yes"}}

    monkeypatch.setattr(instagram_dms, "evaluate_thread_for_creator", _evaluate)

    stats = instagram_dms.ingest_webhook_payload(
        _payload(account_id="acct-a", mid="m-a", text="paid campaign?")
    )

    assert stats["messages_ingested"] == 1
    assert fake.messages[0]["creator_id"] == "creator-a"
    assert fake.threads[0]["creator_id"] == "creator-a"
    assert calls == [("creator-a", fake.threads[0]["id"])]
    assert all(row.get("creator_id") != "creator-b" for row in fake.messages)


def test_ingest_important_inbound_message_creates_manager_notification(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99", "username": "brandco"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {"mid": "m-collab", "text": "what are your paid collab rates?"},
            }],
        }],
    })
    assert stats["messages_ingested"] == 1
    assert len(fake.messages) == 1
    assert len(fake.notifications) == 1
    note: dict[str, Any] = fake.notifications[0]
    assert note["kind"] == "new_dm"
    assert note["priority"] == "high"
    assert note["source_provider"] == "instagram"
    assert note["source_event_id"] == "instagram:message:m-collab"
    assert note["source_thread_id"] == fake.threads[0]["id"]
    assert note["underlying_type"] == "instagram_dm_message"
    assert note["underlying_id"] == fake.messages[0]["id"]
    assert note["link_path"].startswith("/creator/instagram/dms?thread=")
    assert note["metadata"]["suggested_action"] == "draft_reply"


def test_ingest_attachment_only_message_persists_and_notifies(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99", "username": "brandco"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {
                    "mid": "m-image-only",
                    "attachments": [
                        {"type": "image", "payload": {"url": "https://cdn.example/img"}}
                    ],
                },
            }],
        }],
    })

    assert stats["messages_ingested"] == 1
    assert len(fake.messages) == 1
    assert fake.messages[0]["body"] is None
    assert fake.messages[0]["attachments"][0]["type"] == "image"
    assert len(fake.notifications) == 1
    note = fake.notifications[0]
    assert note["title"] == "new instagram media from @brandco"
    assert note["body"] == "They sent you an Instagram media. I can draft a reply."
    assert note["priority"] == "normal"
    assert note["metadata"]["attachment_types"] == ["image"]


def test_ingest_reel_attachment_message_persists_and_notifies_high_priority(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99", "username": "brandco"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {
                    "mid": "m-reel-only",
                    "attachments": [
                        {
                            "type": "reel",
                            "payload": {"url": "https://instagram.com/reel/example"},
                        }
                    ],
                },
            }],
        }],
    })

    assert stats["messages_ingested"] == 1
    assert fake.messages[0]["body"] is None
    assert fake.messages[0]["attachments"][0]["type"] == "reel"
    assert len(fake.notifications) == 1
    note = fake.notifications[0]
    assert note["title"] == "new instagram reel from @brandco"
    assert note["body"] == "They sent you an Instagram reel. I can draft a reply."
    assert note["priority"] == "high"
    assert note["metadata"]["attachment_types"] == ["reel"]


# ---- outbound message (echo) -----------------------------------------


def test_ingest_recognizes_outbound_echo(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "acct-1"},
                "recipient": {"id": "peer-99"},
                "timestamp": 1699999999000,
                "message": {"mid": "m2", "text": "thanks!", "is_echo": True},
            }],
        }],
    })
    assert stats["messages_ingested"] == 1
    assert fake.messages[0]["direction"] == "outbound"
    # Thread built off the peer id, unread NOT incremented (outbound)
    assert fake.threads[0]["unread_count"] == 0


def test_outgoing_message_does_not_trigger_auto_evaluation(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        instagram_dms,
        "evaluate_thread_for_creator",
        lambda user_id, thread_id: calls.append((user_id, thread_id)) or {"ok": True},
    )

    stats = instagram_dms.ingest_webhook_payload(
        _payload(mid="m-outgoing", text="thanks", is_echo=True)
    )

    assert stats["messages_ingested"] == 1
    assert fake.messages[0]["direction"] == "outbound"
    assert calls == []


# ---- idempotence -----------------------------------------------------


def test_ingest_duplicate_message_is_no_op(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    payload = {
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {"mid": "m-same", "text": "hi"},
            }],
        }],
    }
    instagram_dms.ingest_webhook_payload(payload)
    instagram_dms.ingest_webhook_payload(payload)
    # Second delivery must not create a duplicate row
    assert len(fake.messages) == 1
    # Or inflate the already-created thread's unread count.
    assert fake.threads[0]["unread_count"] == 1
    assert len(fake.notifications) == 0


def test_duplicate_webhook_does_not_trigger_duplicate_auto_evaluation(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    calls: list[tuple[str, str]] = []

    def _evaluate(user_id: str, thread_id: str):
        calls.append((user_id, thread_id))
        return {"ok": True, "state": "success", "evaluation": {"Worth responding?": "no"}}

    monkeypatch.setattr(instagram_dms, "evaluate_thread_for_creator", _evaluate)
    payload = _payload(mid="m-dupe", text="paid collab rates?")

    instagram_dms.ingest_webhook_payload(payload)
    instagram_dms.ingest_webhook_payload(payload)

    assert len(fake.messages) == 1
    assert len(calls) == 1
    assert calls[0] == ("creator-1", fake.threads[0]["id"])


def test_ingest_duplicate_important_message_dedupes_notification(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    payload = {
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99", "username": "brandco"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {"mid": "m-same", "text": "paid collab rates?"},
            }],
        }],
    }
    instagram_dms.ingest_webhook_payload(payload)
    instagram_dms.ingest_webhook_payload(payload)
    assert len(fake.messages) == 1
    assert fake.threads[0]["unread_count"] == 1
    assert len(fake.notifications) == 1


def test_new_business_message_re_evaluates_existing_thread(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    fake.threads = [
        {
            "id": "thread-existing",
            "creator_id": "creator-1",
            "ig_thread_id": "peer-99",
            "ig_peer_user_id": "peer-99",
            "peer_username": "brandco",
            "unread_count": 0,
        }
    ]
    fake.messages = [
        {
            "id": "old-message",
            "thread_id": "thread-existing",
            "creator_id": "creator-1",
            "ig_message_id": "m-old",
            "direction": "inbound",
            "sender_ig_id": "peer-99",
            "body": "love your work",
            "attachments": [],
            "received_at": "2026-09-10T01:00:00Z",
        }
    ]
    fake.evaluations = [
        {
            "id": "eval-old",
            "creator_id": "creator-1",
            "thread_id": "thread-existing",
            "message_id": "old-message",
            "result": {"Worth responding?": "no", "Summary": "casual compliment"},
        }
    ]
    prompts = _mock_claude_evaluation(
        monkeypatch,
        worth="yes",
        summary="new paid campaign request needs a reply",
    )

    stats = instagram_dms.ingest_webhook_payload(
        _payload(mid="m-new-business", text="Can you send rates for a paid campaign?")
    )

    assert stats["messages_ingested"] == 1
    assert len(prompts) == 1
    assert "paid campaign" in prompts[0]
    assert len(fake.evaluations) == 2
    assert fake.evaluations[-1]["thread_id"] == "thread-existing"
    assert fake.evaluations[-1]["result"]["Worth responding?"] == "yes"


# ---- malformed messaging entries survive -----------------------------


def test_ingest_skips_message_without_mid(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [
                # Missing message.mid
                {
                    "sender": {"id": "peer-99"},
                    "recipient": {"id": "acct-1"},
                    "message": {"text": "no mid here"},
                },
                # Valid one right after — must still land
                {
                    "sender": {"id": "peer-99"},
                    "recipient": {"id": "acct-1"},
                    "timestamp": 1699999999000,
                    "message": {"mid": "m-real", "text": "real one"},
                },
            ],
        }],
    })
    assert len(fake.messages) == 1
    assert fake.messages[0]["ig_message_id"] == "m-real"


def test_ingest_skips_unsupported_webhook_event(monkeypatch, caplog) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]

    with caplog.at_level(logging.INFO):
        stats = instagram_dms.ingest_webhook_payload({
            "object": "instagram",
            "entry": [{
                "id": "acct-1",
                "messaging": [{
                    "sender": {"id": "peer-99"},
                    "recipient": {"id": "acct-1"},
                    "timestamp": 1699999999000,
                    "read": {"watermark": 1699999999000},
                }],
            }],
        })

    assert stats["messages_ingested"] == 0
    assert fake.messages == []
    assert fake.notifications == []
    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "instagram_dms.message.dropped reason=unsupported_event" in log_text
    assert "read" in log_text


def test_unsupported_webhook_event_does_not_trigger_auto_evaluation(
    monkeypatch,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        instagram_dms,
        "evaluate_thread_for_creator",
        lambda user_id, thread_id: calls.append((user_id, thread_id)) or {"ok": True},
    )

    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "read": {"watermark": 1699999999000},
            }],
        }],
    })

    assert stats["messages_ingested"] == 0
    assert fake.messages == []
    assert calls == []


# ---- crashes never propagate ----------------------------------------


def test_auto_evaluation_failure_keeps_persisted_message(
    monkeypatch,
    caplog,
) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]

    def _raise(_user_id: str, _thread_id: str):
        raise RuntimeError("evaluator down")

    monkeypatch.setattr(instagram_dms, "evaluate_thread_for_creator", _raise)

    with caplog.at_level(logging.ERROR):
        stats = instagram_dms.ingest_webhook_payload(
            _payload(mid="m-eval-fails", text="paid campaign?")
        )

    assert stats["messages_ingested"] == 1
    assert stats["errors"] == 0
    assert len(fake.messages) == 1
    assert fake.evaluations == []
    assert any("instagram_dms.auto_evaluation.failed" in rec.message for rec in caplog.records)


def test_ingest_survives_supabase_crash_on_resolve(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.raise_on = {"oauth_connections"}
    # Should not raise; entry is dropped with no crash.
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {"mid": "m1", "text": "hi"},
            }],
        }],
    })
    # dropped_no_creator = 1 because resolve returned None (via caught exception)
    assert stats["dropped_no_creator"] == 1


# ---- timestamp coercion ---------------------------------------------


def test_timestamp_missing_falls_back_to_now(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99"},
                "recipient": {"id": "acct-1"},
                # timestamp missing
                "message": {"mid": "m1", "text": "hi"},
            }],
        }],
    })
    assert len(fake.messages) == 1
    # received_at got set to something valid (isoformat with 'T')
    assert "T" in fake.messages[0]["received_at"]


def test_unread_count_for_creator_sums_positive_unread(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.threads = [
        {"id": "t1", "creator_id": "c1", "unread_count": 3},
        {"id": "t2", "creator_id": "c1", "unread_count": 1},
        # 0-unread threads are filtered out server-side by the gt(0) clause.
        {"id": "t3", "creator_id": "c1", "unread_count": 0},
        # Another creator's threads are ignored.
        {"id": "t4", "creator_id": "other", "unread_count": 7},
    ]
    assert instagram_dms.unread_count_for_creator("c1") == 4


def test_unread_count_for_creator_zero_when_no_threads(monkeypatch) -> None:
    _install(monkeypatch)
    assert instagram_dms.unread_count_for_creator("c1") == 0


def test_unread_count_for_creator_swallows_error(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.raise_on = {"instagram_dm_threads"}
    assert instagram_dms.unread_count_for_creator("c1") == 0


# ---- direction detection: strict triangulation (additive) ------------


def test_ingest_outbound_by_sender_match_without_echo(monkeypatch) -> None:
    """sender == connected IG account (no is_echo flag) is still
    outbound; unread must not increment, no notification."""
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    stats = instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "acct-1"},
                "recipient": {"id": "peer-9"},
                "timestamp": 1699999999000,
                "message": {"mid": "m-strict-out", "text": "hey"},
            }],
        }],
    })
    assert stats["messages_ingested"] == 1
    assert fake.messages[0]["direction"] == "outbound"
    assert fake.threads[0]["unread_count"] == 0


def test_ingest_inbound_only_when_recipient_is_connected_account(monkeypatch) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-1",
            "messaging": [{
                "sender": {"id": "peer-99"},
                "recipient": {"id": "acct-1"},
                "timestamp": 1699999999000,
                "message": {"mid": "m-strict-in", "text": "hi"},
            }],
        }],
    })
    assert fake.messages[0]["direction"] == "inbound"
    assert fake.threads[0]["unread_count"] == 1


def test_ingest_treats_messaging_recipient_id_variant_as_inbound(monkeypatch) -> None:
    """Real Meta payload: entry.id (owner id) != messaging.recipient.id
    because Instagram Login API uses two id-spaces. The message was
    still routed to us via entry.id, so a peer sender means the
    creator is the recipient. Must be classified inbound."""
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-owner"}
    ]
    instagram_dms.ingest_webhook_payload({
        "object": "instagram",
        "entry": [{
            "id": "acct-owner",  # our resolved owner id
            "messaging": [{
                "sender": {"id": "peer-99"},        # not us
                "recipient": {"id": "acct-msg-recipient-id"},  # different id-space
                "timestamp": 1699999999000,
                "message": {"mid": "m-strict-inbound", "text": "hi"},
            }],
        }],
    })
    assert fake.messages[0]["direction"] == "inbound"
    assert fake.threads[0]["unread_count"] == 1


def test_ingest_drops_self_to_self_ambiguity(monkeypatch, caplog) -> None:
    fake = _install(monkeypatch)
    fake.oauth_rows = [
        {"user_id": "creator-1", "provider": "instagram", "provider_account_id": "acct-1"}
    ]
    import logging
    with caplog.at_level(logging.INFO):
        stats = instagram_dms.ingest_webhook_payload({
            "object": "instagram",
            "entry": [{
                "id": "acct-1",
                "messaging": [{
                    "sender": {"id": "acct-1"},
                    "recipient": {"id": "acct-1"},
                    "timestamp": 1699999999000,
                    "message": {"mid": "m-self", "text": "??"},
                }],
            }],
        })
    assert stats["messages_ingested"] == 0
    assert fake.messages == []
    assert any("ambiguous_direction" in rec.message for rec in caplog.records)
