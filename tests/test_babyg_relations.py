"""Tests for app/services/babyg_relations.py.

Phase 6 of the babyg AI v2 plan (see docs/babyg-ai-reference.md).

These tests prove:

    * A DM from @vansbrand and an email to team@vans.example thread
      onto the same deal, not two duplicates.
    * A new handle seen on a known brand auto-adds to that brand's
      handles list.
    * Terminal deals never absorb new identity signals — a paid Vans
      deal must not attract new touches; those belong on the next
      deal.
    * Case and whitespace do not fool the resolver: "@Vansbrand",
      "vansbrand", " vansbrand " all resolve to the same identity.
    * relationship_notes save + list scoped by creator_id, filterable
      by brand and kind.
    * A note refuses to save without at least one target (brand /
      brand_id / peer_id).
"""

from __future__ import annotations

from typing import Any

import pytest

from app.services import babyg_deals, babyg_relations

_CREATOR = "00000000-0000-0000-0000-000000000010"
_OTHER = "00000000-0000-0000-0000-0000000000cc"


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
        return f"00000000-0000-0000-0000-{n:012d}"

    def table(self, name: str) -> _FakeQuery:
        return _FakeQuery(self, name)


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    s = _FakeStore()
    monkeypatch.setattr(babyg_relations.supabase_client, "get_service_client", lambda: s)
    monkeypatch.setattr(babyg_deals.supabase_client, "get_service_client", lambda: s)
    return s


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_by_brand_name(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    got = babyg_relations.resolve(_CREATOR, brand_name="Vans")
    assert got is not None
    assert got["id"] == deal["id"]


def test_resolve_case_and_whitespace_insensitive(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    for query in ("vans", "  VANS  ", "Vans"):
        got = babyg_relations.resolve(_CREATOR, brand_name=query)
        assert got is not None and got["id"] == deal["id"]


def test_resolve_by_handle(store: _FakeStore) -> None:
    deal = babyg_deals.find_or_create_deal(
        _CREATOR, brand_name="Vans", handles=["vansbrand"]
    )
    assert deal
    got = babyg_relations.resolve(_CREATOR, handle="@Vansbrand")
    assert got is not None
    assert got["id"] == deal["id"]


def test_resolve_by_email(store: _FakeStore) -> None:
    deal = babyg_deals.find_or_create_deal(
        _CREATOR, brand_name="Vans", emails=["team@vans.example"]
    )
    assert deal
    got = babyg_relations.resolve(_CREATOR, email="TEAM@vans.example")
    assert got is not None
    assert got["id"] == deal["id"]


def test_resolve_returns_none_when_unknown(store: _FakeStore) -> None:
    assert babyg_relations.resolve(_CREATOR, brand_name="Nike") is None
    assert babyg_relations.resolve(_CREATOR, handle="strangers") is None


def test_resolve_ignores_terminal_deals(store: _FakeStore) -> None:
    """A paid or declined deal must never resolve. If Vans comes back
    after paying, that's a new deal, not the old one."""
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    babyg_deals.update_stage(deal["id"], "negotiating", creator_id=_CREATOR)
    babyg_deals.update_stage(deal["id"], "declined", creator_id=_CREATOR)
    assert babyg_relations.resolve(_CREATOR, brand_name="Vans") is None


def test_resolve_scoped_per_creator(store: _FakeStore) -> None:
    """Another creator's Vans deal is invisible."""
    other_deal = babyg_deals.create_deal(_OTHER, brand_name="Vans")
    assert other_deal
    assert babyg_relations.resolve(_CREATOR, brand_name="Vans") is None


def test_resolve_no_signal_returns_none(store: _FakeStore) -> None:
    # Nothing to match on. The resolver refuses instead of returning
    # some arbitrary "newest" deal.
    babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert babyg_relations.resolve(_CREATOR) is None


# ---------------------------------------------------------------------------
# learn_identity()
# ---------------------------------------------------------------------------


def test_learn_identity_adds_new_handle(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    got = babyg_relations.learn_identity(
        deal["id"], creator_id=_CREATOR, handle="@vansbrand"
    )
    assert got is not None
    assert "vansbrand" in got["handles"]
    # And the resolver now finds the deal via that handle.
    found = babyg_relations.resolve(_CREATOR, handle="vansbrand")
    assert found and found["id"] == deal["id"]


def test_learn_identity_dedups(store: _FakeStore) -> None:
    deal = babyg_deals.find_or_create_deal(
        _CREATOR, brand_name="Vans", handles=["vansbrand"]
    )
    assert deal
    babyg_relations.learn_identity(
        deal["id"], creator_id=_CREATOR, handle="vansbrand"
    )
    babyg_relations.learn_identity(
        deal["id"], creator_id=_CREATOR, handle="@Vansbrand"
    )
    fresh = babyg_deals.get_deal(deal["id"], creator_id=_CREATOR)
    assert fresh
    assert fresh["handles"].count("vansbrand") == 1


def test_learn_identity_refuses_on_terminal(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    babyg_deals.update_stage(deal["id"], "negotiating", creator_id=_CREATOR)
    babyg_deals.update_stage(deal["id"], "declined", creator_id=_CREATOR)
    out = babyg_relations.learn_identity(
        deal["id"], creator_id=_CREATOR, handle="@vansbrand"
    )
    assert out is None


def test_learn_identity_needs_a_signal(store: _FakeStore) -> None:
    deal = babyg_deals.create_deal(_CREATOR, brand_name="Vans")
    assert deal
    assert babyg_relations.learn_identity(deal["id"], creator_id=_CREATOR) is None


# ---------------------------------------------------------------------------
# thread_touchpoint()
# ---------------------------------------------------------------------------


def test_thread_touchpoint_opens_new_deal_on_first_touch(store: _FakeStore) -> None:
    deal = babyg_relations.thread_touchpoint(
        _CREATOR,
        brand_name="Vans",
        handle="vansbrand",
        kind="dm_message",
        direction="inbound",
        summary="brand slid into IG asking about a collab",
    )
    assert deal is not None
    assert deal["brand_name"] == "Vans"
    assert deal["stage"] == "inquiry"
    assert "vansbrand" in deal["handles"]
    tps = babyg_deals.list_touchpoints(deal["id"], creator_id=_CREATOR)
    assert len(tps) == 1
    assert tps[0]["summary"].startswith("brand slid")


def test_thread_touchpoint_reuses_deal_across_surfaces(store: _FakeStore) -> None:
    """Phase 6 requirement: a DM and an email that match the same
    brand thread onto ONE deal, not two."""
    dm = babyg_relations.thread_touchpoint(
        _CREATOR,
        brand_name="Vans",
        handle="vansbrand",
        kind="dm_message",
        direction="inbound",
        summary="dm",
    )
    email = babyg_relations.thread_touchpoint(
        _CREATOR,
        brand_name="Vans",
        email="team@vans.example",
        kind="email_message",
        direction="inbound",
        summary="email quoting $2k",
        stated_amount_dollars=2000,
    )
    assert dm and email
    assert dm["id"] == email["id"]
    # And the email surface added its identity to the shared deal.
    assert "team@vans.example" in email["emails"]


def test_thread_touchpoint_reuses_deal_when_only_handle_known(store: _FakeStore) -> None:
    """A brand DM's second message may not repeat the brand name, only
    the handle; the resolver still lands on the same deal."""
    first = babyg_relations.thread_touchpoint(
        _CREATOR,
        brand_name="Vans",
        handle="vansbrand",
        kind="dm_message",
        summary="first ping",
    )
    second = babyg_relations.thread_touchpoint(
        _CREATOR,
        handle="@Vansbrand",
        kind="dm_message",
        summary="second ping",
    )
    assert first and second
    assert first["id"] == second["id"]


def test_thread_touchpoint_falls_back_to_handle_as_brand(store: _FakeStore) -> None:
    """First DM with no brand name yet: open a deal seeded from the
    handle so the pipeline shows something. The brand name can be
    upgraded later."""
    deal = babyg_relations.thread_touchpoint(
        _CREATOR,
        handle="mystery_brand",
        kind="dm_message",
        summary="who are you",
    )
    assert deal is not None
    assert deal["brand_name"] == "mystery_brand"


def test_thread_touchpoint_without_any_signal_refuses(store: _FakeStore) -> None:
    out = babyg_relations.thread_touchpoint(
        _CREATOR,
        kind="dm_message",
        summary="unattributed",
    )
    assert out is None


def test_thread_touchpoint_does_not_reopen_declined(store: _FakeStore) -> None:
    """The Phase 5 declined-brand rule holds through Phase 6: a
    declined vans deal must not accept a new touchpoint. thread_touchpoint
    opens a fresh deal instead."""
    first = babyg_relations.thread_touchpoint(
        _CREATOR, brand_name="Vans", handle="vansbrand", kind="dm_message",
        summary="first",
    )
    assert first
    babyg_deals.update_stage(first["id"], "negotiating", creator_id=_CREATOR)
    babyg_deals.update_stage(first["id"], "declined", creator_id=_CREATOR)
    second = babyg_relations.thread_touchpoint(
        _CREATOR, brand_name="Vans", handle="vansbrand", kind="dm_message",
        summary="back again",
    )
    assert second
    assert second["id"] != first["id"]
    assert second["stage"] == "inquiry"


# ---------------------------------------------------------------------------
# Relationship notes
# ---------------------------------------------------------------------------


def test_save_relationship_note_records_row(store: _FakeStore) -> None:
    note = babyg_relations.save_relationship_note(
        _CREATOR,
        kind="payment_reliability",
        body="Vans paid within 14 days in q3",
        brand_name="Vans",
        babyg_source="gmail_thread:abc123",
    )
    assert note is not None
    assert note["kind"] == "payment_reliability"
    assert note["brand_name"] == "Vans"
    assert note["babyg_source"] == "gmail_thread:abc123"


def test_save_note_refuses_unknown_kind(store: _FakeStore) -> None:
    assert (
        babyg_relations.save_relationship_note(
            _CREATOR, kind="astrology", body="mercury", brand_name="Vans"  # type: ignore[arg-type]
        )
        is None
    )


def test_save_note_refuses_without_a_target(store: _FakeStore) -> None:
    """A note without brand_name / brand_id / peer_id is unretrievable
    orphan data. Refuse it."""
    assert (
        babyg_relations.save_relationship_note(
            _CREATOR, kind="other", body="an observation"
        )
        is None
    )


def test_save_note_refuses_empty_body(store: _FakeStore) -> None:
    assert (
        babyg_relations.save_relationship_note(
            _CREATOR, kind="other", body="   ", brand_name="Vans"
        )
        is None
    )


def test_list_notes_by_brand(store: _FakeStore) -> None:
    babyg_relations.save_relationship_note(
        _CREATOR, kind="payment_reliability",
        body="paid on time", brand_name="Vans",
    )
    babyg_relations.save_relationship_note(
        _CREATOR, kind="ghost_history",
        body="ghosted after 3 replies", brand_name="Studio Ferm",
    )
    vans = babyg_relations.list_relationship_notes(_CREATOR, brand_name="vans")
    ferm = babyg_relations.list_relationship_notes(_CREATOR, brand_name="ferm")
    assert [n["brand_name"] for n in vans] == ["Vans"]
    assert [n["brand_name"] for n in ferm] == ["Studio Ferm"]


def test_list_notes_scoped_per_creator(store: _FakeStore) -> None:
    babyg_relations.save_relationship_note(
        _CREATOR, kind="other", body="mine", brand_name="Vans"
    )
    babyg_relations.save_relationship_note(
        _OTHER, kind="other", body="theirs", brand_name="Vans"
    )
    got = babyg_relations.list_relationship_notes(_CREATOR)
    assert [n["body"] for n in got] == ["mine"]


def test_list_notes_filter_by_kind(store: _FakeStore) -> None:
    babyg_relations.save_relationship_note(
        _CREATOR, kind="payment_reliability",
        body="paid", brand_name="Vans",
    )
    babyg_relations.save_relationship_note(
        _CREATOR, kind="ghost_history",
        body="ghosted", brand_name="Vans",
    )
    got = babyg_relations.list_relationship_notes(
        _CREATOR, kind="payment_reliability"
    )
    assert [n["body"] for n in got] == ["paid"]


def test_normalize_handle_strips_at_sign() -> None:
    assert babyg_relations._normalize_handle("@vansbrand") == "vansbrand"
    assert babyg_relations._normalize_handle(" @VANSBRAND ") == "vansbrand"
    assert babyg_relations._normalize_handle(None) == ""
