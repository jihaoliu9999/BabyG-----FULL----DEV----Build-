"""Tests for app/services/babyg_deals.py.

Phase 5 of the babyg AI v2 plan (see docs/babyg-ai-reference.md).

These tests prove:

    * find_or_create_deal returns the same deal for a brand touching us
      twice within the same day.
    * A declined deal never gets re-nudged: touchpoint writes refuse
      and the next brand touch opens a NEW deal, not the closed one.
    * Stage transitions follow the allowed graph. Bad jumps are
      refused, not silently applied.
    * Terminal stages (paid, declined, cancelled) reject follow-up
      writes.
    * Money helpers store cents, not dollars.
    * Brand match is case-insensitive and whitespace-tolerant.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from app.services import babyg_deals

_CREATOR = "00000000-0000-0000-0000-000000000010"
_OTHER = "00000000-0000-0000-0000-0000000000cc"
_DEAL_A = "00000000-0000-0000-0000-0000000000a0"
_DEAL_B = "00000000-0000-0000-0000-0000000000b0"


class _FakeQuery:
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
            row = {
                "id": self._store.next_id(self._table),
                **self._insert_payload,
            }
            self._store.rows.setdefault(self._table, []).append(row)
            return type("Result", (), {"data": [row]})()

        if self._update_payload is not None:
            hit: list[dict[str, Any]] = []
            for row in self._store.rows.get(self._table, []):
                if all(
                    op != "eq" or str(row.get(col)) == str(val)
                    for op, col, val in self._filters
                ):
                    row.update(self._update_payload)
                    hit.append(row)
            return type("Result", (), {"data": hit})()

        rows = list(self._store.rows.get(self._table, []))
        for op, col, val in self._filters:
            if op == "eq":
                rows = [r for r in rows if str(r.get(col)) == str(val)]
        if self._order_col:
            rows.sort(
                key=lambda r: r.get(self._order_col) or "",
                reverse=self._order_desc,
            )
        if self._limit_n:
            rows = rows[: self._limit_n]
        return type("Result", (), {"data": rows})()


class _FakeStore:
    def __init__(self) -> None:
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self._id_seq: dict[str, int] = {}

    def next_id(self, table: str) -> str:
        n = self._id_seq.get(table, 0) + 1
        self._id_seq[table] = n
        # Return a UUID-shaped string so safe_uuid() accepts follow-up
        # calls like get_deal(id) that come from insert results.
        return f"00000000-0000-0000-0000-{n:012d}"

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    monkeypatch.setattr(babyg_deals.supabase_client, "get_service_client", lambda: s)
    return s


# ---------------------------------------------------------------------------
# Stage graph contract
# ---------------------------------------------------------------------------


def test_stage_graph_covers_every_stage() -> None:
    assert set(babyg_deals._STAGE_GRAPH.keys()) == set(babyg_deals.STAGES)


def test_terminal_stages_have_no_transitions() -> None:
    for stage in babyg_deals.TERMINAL_STAGES:
        assert babyg_deals._STAGE_GRAPH[stage] == frozenset()


def test_cannot_jump_inquiry_to_paid() -> None:
    assert babyg_deals.can_transition("inquiry", "paid") is False


def test_cannot_reopen_terminal() -> None:
    assert babyg_deals.can_transition("declined", "negotiating") is False
    assert babyg_deals.can_transition("paid", "delivered") is False
    assert babyg_deals.can_transition("cancelled", "inquiry") is False


def test_same_stage_is_a_noop() -> None:
    # Idempotent writes: callers can re-affirm the current stage without
    # the graph blocking them.
    assert babyg_deals.can_transition("negotiating", "negotiating")


def test_happy_path_allowed() -> None:
    assert babyg_deals.can_transition("inquiry", "negotiating")
    assert babyg_deals.can_transition("negotiating", "accepted")
    assert babyg_deals.can_transition("accepted", "delivered")
    assert babyg_deals.can_transition("delivered", "payment_pending")
    assert babyg_deals.can_transition("payment_pending", "paid")


def test_stale_can_come_back_to_negotiating() -> None:
    assert babyg_deals.can_transition("stale_or_ghosted", "negotiating")


# ---------------------------------------------------------------------------
# create + find_or_create
# ---------------------------------------------------------------------------


def test_create_deal_writes_row(store: _FakeStore) -> None:
    row = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert row is not None
    assert row["brand_name"] == "Vans"
    assert row["stage"] == "inquiry"
    assert row["creator_id"] == _CREATOR


def test_create_deal_refuses_bad_creator(store: _FakeStore) -> None:
    assert babyg_deals.create_deal("not-uuid", brand_name="Vans") is None


def test_create_deal_refuses_bad_stage(store: _FakeStore) -> None:
    assert (
        babyg_deals.create_deal(_CREATOR, brand_name="Vans", stage="bogus")  # type: ignore[arg-type]
        is None
    )


def test_create_deal_refuses_empty_brand(store: _FakeStore) -> None:
    assert babyg_deals.create_deal(_CREATOR, brand_name="   ") is None


def test_find_or_create_same_brand_returns_existing(store: _FakeStore) -> None:
    """Phase 5 requirement: two DMs from the same brand within 24h link
    to the same deal, not two separate ones."""
    first = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Vans")
    second = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Vans")
    assert first is not None and second is not None
    assert first["id"] == second["id"]
    assert len(store.rows["babyg_memory_deals"]) == 1


def test_find_or_create_case_insensitive(store: _FakeStore) -> None:
    a = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Vans")
    b = babyg_deals.find_or_create_deal(_CREATOR, brand_name="vans")
    c = babyg_deals.find_or_create_deal(_CREATOR, brand_name="  VANS  ")
    assert a and b and c
    assert a["id"] == b["id"] == c["id"]


def test_find_or_create_scoped_per_creator(store: _FakeStore) -> None:
    """Two creators can each have their own Vans deal."""
    a = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Vans")
    b = babyg_deals.find_or_create_deal(_OTHER, brand_name="Vans")
    assert a and b
    assert a["id"] != b["id"]


def test_find_or_create_merges_identity(store: _FakeStore) -> None:
    """A second touch with a new handle records the handle so future
    touches on that surface find the same deal."""
    first = babyg_deals.find_or_create_deal(
        _CREATOR, brand_name="Vans", handles=["vansbrand"]
    )
    assert first
    second = babyg_deals.find_or_create_deal(
        _CREATOR, brand_name="Vans", emails=["team@vans.example"]
    )
    assert second
    assert second["handles"] == ["vansbrand"]
    assert second["emails"] == ["team@vans.example"]


def test_declined_brand_returning_opens_new_deal(store: _FakeStore) -> None:
    """A declined deal never gets re-nudged. When the same brand comes
    back weeks later, that's a fresh conversation, not a resurrected
    row."""
    a = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Vans")
    assert a
    babyg_deals.update_stage(a["id"], "negotiating", creator_id=_CREATOR)
    babyg_deals.update_stage(a["id"], "declined", creator_id=_CREATOR)
    b = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Vans")
    assert b
    assert b["id"] != a["id"]
    assert b["stage"] == "inquiry"


# ---------------------------------------------------------------------------
# Stage transitions
# ---------------------------------------------------------------------------


def test_update_stage_writes_the_new_stage(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    out = babyg_deals.update_stage(
        deal["id"], "negotiating", creator_id=_CREATOR
    )
    assert out is not None
    assert out["stage"] == "negotiating"
    fresh = babyg_deals.get_deal(deal["id"], creator_id=_CREATOR)
    assert fresh and fresh["stage"] == "negotiating"


def test_update_stage_refuses_illegal_jump(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    # inquiry -> paid is not allowed. The write must not happen.
    out = babyg_deals.update_stage(
        deal["id"], "paid", creator_id=_CREATOR
    )
    assert out is None
    fresh = babyg_deals.get_deal(deal["id"], creator_id=_CREATOR)
    assert fresh and fresh["stage"] == "inquiry"


def test_update_stage_stores_money_as_cents(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    babyg_deals.update_stage(deal["id"], "negotiating", creator_id=_CREATOR)
    out = babyg_deals.update_stage(
        deal["id"],
        "accepted",
        creator_id=_CREATOR,
        agreed_amount_dollars=2000,
    )
    assert out is not None
    assert out["agreed_amount_cents"] == 200000


def test_update_stage_refuses_unknown_stage(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    assert (
        babyg_deals.update_stage(deal["id"], "unicorn", creator_id=_CREATOR)  # type: ignore[arg-type]
        is None
    )


def test_update_stage_scoped_by_creator(store: _FakeStore) -> None:
    """Another creator's uuid cannot change this deal's stage."""
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    out = babyg_deals.update_stage(deal["id"], "negotiating", creator_id=_OTHER)
    assert out is None


# ---------------------------------------------------------------------------
# Touchpoints
# ---------------------------------------------------------------------------


def test_add_touchpoint_bumps_last_touch(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    original_last_touch = deal["last_touch_at"]
    later = datetime(2027, 1, 1, tzinfo=UTC)
    out = babyg_deals.add_touchpoint(
        deal["id"],
        _CREATOR,
        kind="dm_message",
        summary="brand replied about pricing",
        direction="inbound",
        occurred_at=later,
    )
    assert out is not None
    fresh = babyg_deals.get_deal(deal["id"], creator_id=_CREATOR)
    assert fresh
    assert fresh["last_touch_at"] > original_last_touch


def test_add_touchpoint_stores_stated_amount_as_cents(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    row = babyg_deals.add_touchpoint(
        deal["id"],
        _CREATOR,
        kind="email_message",
        summary="quoted $2k",
        direction="inbound",
        stated_amount_dollars=2000,
    )
    assert row is not None
    assert row["stated_amount_cents"] == 200000


def test_add_touchpoint_refuses_on_terminal_deal(store: _FakeStore) -> None:
    """A declined deal never gets re-nudged, even by a rogue touchpoint
    call from a later background job."""
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    babyg_deals.update_stage(deal["id"], "negotiating", creator_id=_CREATOR)
    babyg_deals.update_stage(deal["id"], "declined", creator_id=_CREATOR)
    out = babyg_deals.add_touchpoint(
        deal["id"],
        _CREATOR,
        kind="dm_message",
        summary="follow up",
    )
    assert out is None


def test_add_touchpoint_bad_kind_refused(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    out = babyg_deals.add_touchpoint(
        deal["id"], _CREATOR, kind="carrier_pigeon", summary="?"  # type: ignore[arg-type]
    )
    assert out is None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


def test_list_deals_orders_by_last_touch(store: _FakeStore) -> None:
    older = babyg_deals.create_deal(_CREATOR, brand_name="Older")
    assert older
    newer = babyg_deals.find_or_create_deal(_CREATOR, brand_name="Newer")
    assert newer
    babyg_deals.add_touchpoint(
        newer["id"], _CREATOR, kind="dm_message", summary="ping"
    )
    got = babyg_deals.list_deals(_CREATOR)
    assert [d["brand_name"] for d in got][:2] == ["Newer", "Older"]


def test_list_deals_active_only_hides_terminal(store: _FakeStore) -> None:
    open_deal = babyg_deals.create_deal(_CREATOR, brand_name="Open")
    closed = babyg_deals.create_deal(_CREATOR, brand_name="Closed")
    assert open_deal and closed
    babyg_deals.update_stage(closed["id"], "negotiating", creator_id=_CREATOR)
    babyg_deals.update_stage(closed["id"], "declined", creator_id=_CREATOR)
    got = babyg_deals.list_deals(_CREATOR, active_only=True)
    assert [d["brand_name"] for d in got] == ["Open"]


def test_list_deals_scoped_per_creator(store: _FakeStore) -> None:
    babyg_deals.create_deal(_CREATOR, brand_name="Mine")
    babyg_deals.create_deal(_OTHER, brand_name="Theirs")
    got = babyg_deals.list_deals(_CREATOR)
    assert [d["brand_name"] for d in got] == ["Mine"]


def test_get_deal_scoped_per_creator(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    assert babyg_deals.get_deal(deal["id"], creator_id=_CREATOR) is not None
    assert babyg_deals.get_deal(deal["id"], creator_id=_OTHER) is None


def test_list_touchpoints_newest_first(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    babyg_deals.add_touchpoint(
        deal["id"], _CREATOR, kind="dm_message", summary="first",
        occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    babyg_deals.add_touchpoint(
        deal["id"], _CREATOR, kind="email_message", summary="second",
        occurred_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    got = babyg_deals.list_touchpoints(deal["id"], creator_id=_CREATOR)
    assert [t["summary"] for t in got] == ["second", "first"]


# ---------------------------------------------------------------------------
# Money helpers
# ---------------------------------------------------------------------------


def test_dollars_to_cents_rounds_and_ignores_none() -> None:
    assert babyg_deals._dollars_to_cents(None) is None
    assert babyg_deals._dollars_to_cents(0) == 0
    assert babyg_deals._dollars_to_cents(2000) == 200000
    assert babyg_deals._dollars_to_cents(19.99) == 1999
    assert babyg_deals._dollars_to_cents("not a number") is None  # type: ignore[arg-type]
