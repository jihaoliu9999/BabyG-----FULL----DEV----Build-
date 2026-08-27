"""Tests for app/services/babyg_memory.py.

Phase 3 of the babyg AI v2 plan (see docs/babyg-ai-reference.md).

These tests cover the memory service contract without hitting a real
Supabase. A fake service client captures every insert and returns
canned rows on select. That lets us prove:

    * save() routes to the right table for each kind
    * unknown kinds are refused, not silently redirected
    * bad creator_ids return None / [] instead of raising
    * read() filters by creator_id and orders newest first
    * read_for_operator() writes an audit row BEFORE returning data
    * a failed audit write means no data is returned to the operator
    * kind enum stays aligned with the migration files
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import babyg_memory

# Stable UUIDs for tests.
_CREATOR_ID = "00000000-0000-0000-0000-000000000001"
_OPERATOR_ID = "00000000-0000-0000-0000-000000000002"


class _FakeQuery:
    """Chainable stub for the Supabase query builder used by babyg_memory."""

    def __init__(self, store: _FakeStore, table: str) -> None:
        self._store = store
        self._table = table
        self._filters: list[tuple[str, str, Any]] = []
        self._order_col: str | None = None
        self._order_desc = True
        self._limit_n: int | None = None
        self._insert_payload: dict[str, Any] | None = None
        self._update_payload: dict[str, Any] | None = None

    def select(self, _cols: str) -> _FakeQuery:
        return self

    def eq(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append(("eq", col, val))
        return self

    def gte(self, col: str, val: Any) -> _FakeQuery:
        self._filters.append(("gte", col, val))
        return self

    def order(self, col: str, *, desc: bool = True) -> _FakeQuery:
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n: int) -> _FakeQuery:
        self._limit_n = n
        return self

    def insert(self, payload: dict[str, Any]) -> _FakeQuery:
        self._insert_payload = payload
        return self

    def update(self, payload: dict[str, Any]) -> _FakeQuery:
        self._update_payload = payload
        return self

    def execute(self) -> Any:
        if self._insert_payload is not None:
            row = {"id": f"row-{len(self._store.inserts[self._table]) + 1}",
                   **self._insert_payload}
            self._store.inserts[self._table].append(row)
            self._store.rows.setdefault(self._table, []).append(row)
            if self._store.fail_insert_tables and self._table in self._store.fail_insert_tables:
                raise RuntimeError(f"synthetic insert fail for {self._table}")
            return type("Result", (), {"data": [row]})()

        if getattr(self, "_update_payload", None) is not None:
            updated: list[dict[str, Any]] = []
            for row in self._store.rows.get(self._table, []):
                if all(
                    op != "eq" or str(row.get(col)) == str(val)
                    for op, col, val in self._filters
                ):
                    row.update(self._update_payload)
                    updated.append(row)
            self._store.updates.setdefault(self._table, []).extend(updated)
            return type("Result", (), {"data": updated})()

        rows = list(self._store.rows.get(self._table, []))
        for op, col, val in self._filters:
            if op == "eq":
                rows = [r for r in rows if str(r.get(col)) == str(val)]
            elif op == "gte":
                rows = [r for r in rows if r.get(col) is not None and r[col] >= val]
        if self._order_col:
            rows.sort(key=lambda r: r.get(self._order_col) or "", reverse=self._order_desc)
        if self._limit_n:
            rows = rows[: self._limit_n]
        return type("Result", (), {"data": rows})()


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.inserts: dict[str, list[dict[str, Any]]] = {}
        self.updates: dict[str, list[dict[str, Any]]] = {}
        self.fail_insert_tables: set[str] = set()

    def seed(self, table: str, rows: list[dict[str, Any]]) -> None:
        self.rows.setdefault(table, []).extend(rows)

    def table(self, name: str) -> _FakeQuery:
        self.inserts.setdefault(name, [])
        return _FakeQuery(self, name)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    monkeypatch.setattr(babyg_memory.supabase_client, "get_service_client", lambda: s)
    return s


# ---------------------------------------------------------------------------
# Contract: kind -> table map matches the migration files.
# ---------------------------------------------------------------------------


def test_kind_table_map_covers_every_kind() -> None:
    expected_kinds = {
        "drafts", "decisions", "deals", "deal_touchpoints",
        "voice_samples", "contract_flags", "relationship_notes",
        "creator_preferences",
    }
    assert set(babyg_memory._KIND_TABLE.keys()) == expected_kinds


def test_every_kind_has_a_date_column() -> None:
    for kind in babyg_memory._KIND_TABLE:
        assert kind in babyg_memory._KIND_DATE_COLUMN


# ---------------------------------------------------------------------------
# save()
# ---------------------------------------------------------------------------


def test_save_routes_to_right_table(store: _FakeStore) -> None:
    row = babyg_memory.save(
        "decisions",
        _CREATOR_ID,
        {"kind": "counter_sent", "summary": "counter Vans at $2k"},
    )
    assert row is not None
    assert store.inserts["babyg_memory_decisions"], "decision should have been inserted"
    inserted = store.inserts["babyg_memory_decisions"][0]
    assert inserted["creator_id"] == _CREATOR_ID
    assert inserted["summary"] == "counter Vans at $2k"


def test_save_unknown_kind_refuses(store: _FakeStore) -> None:
    row = babyg_memory.save("bogus_kind", _CREATOR_ID, {"x": 1})  # type: ignore[arg-type]
    assert row is None
    # No table was touched.
    assert store.inserts == {}


def test_save_bad_creator_id_returns_none(store: _FakeStore) -> None:
    row = babyg_memory.save("decisions", "not-a-uuid", {"summary": "x"})
    assert row is None
    assert store.inserts == {}


# ---------------------------------------------------------------------------
# read()
# ---------------------------------------------------------------------------


def test_read_returns_only_creators_rows(store: _FakeStore) -> None:
    other_creator = "00000000-0000-0000-0000-0000000000ff"
    now = datetime.now(UTC).isoformat()
    store.seed("babyg_memory_decisions", [
        {"id": "a", "creator_id": _CREATOR_ID, "summary": "mine", "created_at": now},
        {"id": "b", "creator_id": other_creator, "summary": "someone else", "created_at": now},
    ])
    got = babyg_memory.read("decisions", _CREATOR_ID)
    summaries = [r["summary"] for r in got]
    assert summaries == ["mine"]


def test_read_since_filters(store: _FakeStore) -> None:
    now = datetime.now(UTC)
    old = (now - timedelta(days=400)).isoformat()
    fresh = (now - timedelta(days=5)).isoformat()
    store.seed("babyg_memory_decisions", [
        {"id": "old", "creator_id": _CREATOR_ID, "summary": "old", "created_at": old},
        {"id": "new", "creator_id": _CREATOR_ID, "summary": "new", "created_at": fresh},
    ])
    got = babyg_memory.read(
        "decisions",
        _CREATOR_ID,
        since=(now - timedelta(days=30)),
    )
    assert [r["summary"] for r in got] == ["new"]


def test_read_bad_creator_returns_empty(store: _FakeStore) -> None:
    assert babyg_memory.read("decisions", "not-uuid") == []


def test_read_unknown_kind_returns_empty(store: _FakeStore) -> None:
    assert babyg_memory.read("bogus", _CREATOR_ID) == []  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Operator access: audit-before-read.
# ---------------------------------------------------------------------------


def test_read_for_operator_writes_audit_before_returning_data(store: _FakeStore) -> None:
    store.seed("babyg_memory_decisions", [
        {"id": "d1", "creator_id": _CREATOR_ID, "summary": "x",
         "created_at": datetime.now(UTC).isoformat()},
    ])
    rows = babyg_memory.read_for_operator(
        "decisions",
        _CREATOR_ID,
        operator_id=_OPERATOR_ID,
        reason="abuse review case #42",
    )
    assert len(rows) == 1
    audit_rows = store.inserts.get("memory_access_audit", [])
    assert len(audit_rows) == 1
    audit = audit_rows[0]
    assert audit["operator_id"] == _OPERATOR_ID
    assert audit["creator_id"] == _CREATOR_ID
    assert audit["memory_kind"] == "decisions"
    assert audit["reason"] == "abuse review case #42"
    assert audit["memory_row_ids"] == ["d1"]


def test_read_for_operator_refuses_without_reason(store: _FakeStore) -> None:
    store.seed("babyg_memory_decisions", [
        {"id": "d1", "creator_id": _CREATOR_ID, "summary": "x",
         "created_at": datetime.now(UTC).isoformat()},
    ])
    rows = babyg_memory.read_for_operator(
        "decisions",
        _CREATOR_ID,
        operator_id=_OPERATOR_ID,
        reason="   ",
    )
    assert rows == []
    assert store.inserts.get("memory_access_audit", []) == []


def test_read_for_operator_no_data_when_audit_fails(store: _FakeStore) -> None:
    store.seed("babyg_memory_decisions", [
        {"id": "d1", "creator_id": _CREATOR_ID, "summary": "x",
         "created_at": datetime.now(UTC).isoformat()},
    ])
    store.fail_insert_tables.add("memory_access_audit")
    rows = babyg_memory.read_for_operator(
        "decisions",
        _CREATOR_ID,
        operator_id=_OPERATOR_ID,
        reason="fraud review",
    )
    # Even though the underlying read succeeded, we refuse to hand the
    # data over if the audit did not persist.
    assert rows == []


def test_read_for_operator_refuses_unknown_kind(store: _FakeStore) -> None:
    rows = babyg_memory.read_for_operator(
        "bogus",  # type: ignore[arg-type]
        _CREATOR_ID,
        operator_id=_OPERATOR_ID,
        reason="asked for it",
    )
    assert rows == []
    assert store.inserts.get("memory_access_audit", []) == []


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def test_read_recent_summary_counts_every_kind(store: _FakeStore) -> None:
    now = datetime.now(UTC).isoformat()
    store.seed("babyg_memory_decisions", [
        {"id": "a", "creator_id": _CREATOR_ID, "summary": "x", "created_at": now},
        {"id": "b", "creator_id": _CREATOR_ID, "summary": "y", "created_at": now},
    ])
    store.seed("babyg_memory_drafts", [
        {"id": "d1", "creator_id": _CREATOR_ID, "body": "hi", "created_at": now, "status": "proposed", "channel": "email"},
    ])
    summary = babyg_memory.read_recent_summary(_CREATOR_ID)
    assert summary["decisions"] == 2
    assert summary["drafts"] == 1
    # Every kind appears with a non-negative int, even zero.
    for kind in babyg_memory._KIND_TABLE:
        assert kind in summary
        assert isinstance(summary[kind], int)


# ---------------------------------------------------------------------------
# Drafts (Phase 4)
# ---------------------------------------------------------------------------


def test_save_draft_writes_row_and_returns_id(store: _FakeStore) -> None:
    draft_id = babyg_memory.save_draft(
        _CREATOR_ID,
        channel="email",
        origin_tool="gmail.create_draft",
        subject="re: collab",
        to_addr="vans@example.test",
        body="hey team, happy to chat thursday.",
    )
    assert draft_id is not None
    rows = store.inserts["babyg_memory_drafts"]
    assert len(rows) == 1
    row = rows[0]
    assert row["creator_id"] == _CREATOR_ID
    assert row["channel"] == "email"
    assert row["origin_tool"] == "gmail.create_draft"
    assert row["subject"] == "re: collab"
    assert row["to_addr"] == "vans@example.test"
    assert row["status"] == "proposed"
    # Gmail thread ids are strings, not uuids — thread_id must be absent
    # when not a valid uuid, so we can't corrupt the column.
    assert "thread_id" not in row


def test_save_draft_refuses_bad_channel(store: _FakeStore) -> None:
    draft_id = babyg_memory.save_draft(
        _CREATOR_ID,
        channel="carrier_pigeon",  # type: ignore[arg-type]
        origin_tool="none",
        subject="s",
        to_addr="a@b",
        body="hi",
    )
    assert draft_id is None
    assert store.inserts.get("babyg_memory_drafts", []) == []


def test_save_draft_refuses_empty_body(store: _FakeStore) -> None:
    draft_id = babyg_memory.save_draft(
        _CREATOR_ID,
        channel="email",
        origin_tool="gmail.create_draft",
        subject="s",
        to_addr="a@b",
        body="   ",
    )
    assert draft_id is None


def test_save_draft_refuses_bad_creator(store: _FakeStore) -> None:
    draft_id = babyg_memory.save_draft(
        "not-a-uuid",
        channel="email",
        origin_tool="gmail.create_draft",
        subject="s",
        to_addr="a@b",
        body="hi",
    )
    assert draft_id is None


def test_update_draft_status_flips_status_and_stamps_sent(store: _FakeStore) -> None:
    draft_id = babyg_memory.save_draft(
        _CREATOR_ID,
        channel="email",
        origin_tool="gmail.send_email",
        subject="ok",
        to_addr="vans@example.test",
        body="lets do it",
    )
    assert draft_id
    ok = babyg_memory.update_draft_status(
        draft_id, "sent", gmail_message_id="gmail-msg-123"
    )
    assert ok is True
    row = store.rows["babyg_memory_drafts"][0]
    assert row["status"] == "sent"
    assert row["gmail_message_id"] == "gmail-msg-123"
    assert row["sent_at"]  # stamped
    assert row["updated_at"]


def test_update_draft_status_rejects_bad_status(store: _FakeStore) -> None:
    draft_id = babyg_memory.save_draft(
        _CREATOR_ID,
        channel="email",
        origin_tool="gmail.create_draft",
        subject="s",
        to_addr="a@b",
        body="hi",
    )
    assert draft_id
    assert not babyg_memory.update_draft_status(draft_id, "yeeted")  # type: ignore[arg-type]
    row = store.rows["babyg_memory_drafts"][0]
    assert row["status"] == "proposed"


def test_list_drafts_finds_by_substring(store: _FakeStore) -> None:
    babyg_memory.save_draft(
        _CREATOR_ID,
        channel="email",
        origin_tool="gmail.create_draft",
        subject="re: Vans collab",
        to_addr="vans@example.test",
        body="hey vans team",
    )
    babyg_memory.save_draft(
        _CREATOR_ID,
        channel="email",
        origin_tool="gmail.create_draft",
        subject="Olipop pitch",
        to_addr="olipop@example.test",
        body="excited about olipop",
    )
    hits = babyg_memory.list_drafts(_CREATOR_ID, match="vans")
    assert len(hits) == 1
    assert hits[0]["subject"] == "re: Vans collab"


def test_list_drafts_filters_by_status(store: _FakeStore) -> None:
    d1 = babyg_memory.save_draft(
        _CREATOR_ID, channel="email", origin_tool="gmail.create_draft",
        subject="a", to_addr="a@b", body="a",
    )
    d2 = babyg_memory.save_draft(
        _CREATOR_ID, channel="email", origin_tool="gmail.create_draft",
        subject="b", to_addr="a@b", body="b",
    )
    assert d1 and d2
    babyg_memory.update_draft_status(d2, "canceled")
    proposed = babyg_memory.list_drafts(_CREATOR_ID, status="proposed")
    canceled = babyg_memory.list_drafts(_CREATOR_ID, status="canceled")
    assert [r["subject"] for r in proposed] == ["a"]
    assert [r["subject"] for r in canceled] == ["b"]


def test_list_drafts_scoped_to_creator(store: _FakeStore) -> None:
    other = "00000000-0000-0000-0000-0000000000cc"
    babyg_memory.save_draft(
        _CREATOR_ID, channel="email", origin_tool="gmail.create_draft",
        subject="mine", to_addr="a@b", body="body",
    )
    babyg_memory.save_draft(
        other, channel="email", origin_tool="gmail.create_draft",
        subject="theirs", to_addr="a@b", body="body",
    )
    mine = babyg_memory.list_drafts(_CREATOR_ID)
    assert [r["subject"] for r in mine] == ["mine"]


def test_list_drafts_bad_creator_returns_empty(store: _FakeStore) -> None:
    assert babyg_memory.list_drafts("nope") == []


def test_default_preload_cutoff_is_365_days_ago() -> None:
    now = datetime.now(UTC)
    cutoff = babyg_memory.default_preload_cutoff()
    delta = now - cutoff
    # Between 364 and 366 days to allow for test-run drift.
    assert timedelta(days=364) <= delta <= timedelta(days=366)
