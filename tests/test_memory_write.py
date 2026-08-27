"""Tests for app/agent/tools/memory_write.py.

Phase 9 of the babyg AI v2 plan (see docs/babyg-ai-reference.md).

The `remember` tool writes to babyg's own memory. It must never trigger
an external side effect — no Gmail send, no calendar create, no DM
insert. That property is proven two ways here:

    1. Explicit kind allowlist. Kinds like 'drafts', 'deals',
       'deal_touchpoints' are refused with a helpful reason.
    2. Import audit. The module deliberately does not import gmail,
       calendar, or dms services — a prompt-injection payload that
       tricked the model into calling remember has no path to reach
       them.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from app.agent.tools import memory_write


def test_remember_allowlist_matches_spec() -> None:
    assert frozenset(
        {
            "decisions",
            "creator_preferences",
            "voice_samples",
            "relationship_notes",
            "contract_flags",
        }
    ) == memory_write._ALLOWED_KINDS


def test_remember_refuses_forbidden_kinds() -> None:
    for kind in ("drafts", "deals", "deal_touchpoints"):
        out = memory_write.remember("u", {"kind": kind, "summary": "x"})
        assert out["ok"] is False
        assert "route through the deal flow" in out["reason"]


def test_remember_refuses_unknown_kind() -> None:
    out = memory_write.remember("u", {"kind": "astrology", "summary": "x"})
    assert out["ok"] is False


def test_remember_refuses_empty_summary() -> None:
    out = memory_write.remember("u", {"kind": "decisions", "summary": "   "})
    assert out["ok"] is False


def test_remember_writes_decision(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _fake_save(kind, uid, payload):
        captured["kind"] = kind
        captured["uid"] = uid
        captured["payload"] = payload
        return {"id": "row-1", **payload}

    monkeypatch.setattr(memory_write.babyg_memory, "save", _fake_save)
    out = memory_write.remember(
        "u-1",
        {"kind": "decisions", "summary": "passed on Nike gifting"},
    )
    assert out == {"ok": True, "kind": "decisions", "id": "row-1"}
    assert captured["kind"] == "decisions"
    assert captured["payload"]["summary"] == "passed on Nike gifting"


def test_remember_routes_voice_sample_to_sample_field(monkeypatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        memory_write.babyg_memory,
        "save",
        lambda kind, uid, payload: captured.setdefault("payload", payload) or {"id": "v1"},
    )
    memory_write.remember(
        "u-1",
        {"kind": "voice_samples", "summary": "hey team, quick one —"},
    )
    assert captured["payload"]["sample"] == "hey team, quick one —"
    assert captured["payload"]["channel"] == "chat"


def test_remember_relationship_note_needs_brand(monkeypatch) -> None:
    monkeypatch.setattr(
        memory_write.babyg_relations,
        "save_relationship_note",
        lambda *a, **kw: pytest.fail("must not save without brand"),
    )
    out = memory_write.remember(
        "u-1",
        {"kind": "relationship_notes", "summary": "paid on time"},
    )
    assert out["ok"] is False
    assert "brand_name" in out["reason"]


def test_remember_relationship_note_routes_through_relations(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _save_note(uid, *, kind, body, brand_name=None, brand_id=None,
                   peer_id=None, babyg_source=None):
        captured.update({
            "uid": uid, "kind": kind, "body": body,
            "brand_name": brand_name, "babyg_source": babyg_source,
        })
        return {"id": "n1"}

    monkeypatch.setattr(
        memory_write.babyg_relations, "save_relationship_note", _save_note
    )
    out = memory_write.remember(
        "u-1",
        {
            "kind": "relationship_notes",
            "summary": "Vans paid within 14 days q3",
            "brand_name": "Vans",
            "note_kind": "payment_reliability",
        },
    )
    assert out == {"ok": True, "kind": "relationship_notes", "id": "n1"}
    assert captured["brand_name"] == "Vans"
    assert captured["kind"] == "payment_reliability"
    assert captured["babyg_source"] == "remember_tool"


def test_remember_relationship_note_defaults_kind_to_other(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def _save_note(uid, *, kind, **kw):
        captured["kind"] = kind
        return {"id": "n"}

    monkeypatch.setattr(
        memory_write.babyg_relations, "save_relationship_note", _save_note
    )
    memory_write.remember(
        "u-1",
        {
            "kind": "relationship_notes",
            "summary": "observation",
            "brand_name": "Olipop",
        },
    )
    assert captured["kind"] == "other"


def test_remember_module_never_imports_external_write_paths() -> None:
    """Import audit: this module must not depend on gmail, calendar,
    or dms services. If it does, a prompt-injection that persuades the
    model to call `remember` could be escalated to an external write."""
    src = inspect.getsource(memory_write)
    banned = [
        "google_gmail",
        "google_calendar",
        "app.integrations",
        "from app.services import dms",
        "from app.services import action_proposals",
        "from app.services import bookings",
    ]
    for token in banned:
        assert token not in src, (
            f"memory_write must not import {token!r}; that would let a "
            "prompt-injection escalate remember into an external side effect."
        )


def test_remember_returns_failure_when_save_fails(monkeypatch) -> None:
    monkeypatch.setattr(memory_write.babyg_memory, "save", lambda *a, **kw: None)
    out = memory_write.remember(
        "u-1", {"kind": "decisions", "summary": "x"}
    )
    assert out == {"ok": False, "reason": "decisions write failed"}
