"""Proactive nudges from babyg.

Locks in the ai-manager behavior: babyg inserts an assistant message
into the user's bot thread whenever a fresh discover match or an
imminent pending booking shows up, and it only does so once per
underlying entity (deduped by ``tool_calls.nudge_key``).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services import bookings as bookings_module
from app.services import bot as bot_module
from app.services import bot_nudges
from app.services import discover as discover_module


@pytest.fixture()
def stub_inserts(monkeypatch):
    """Capture create_message + list_messages so we don't touch Supabase."""
    inserted: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    def _create(**body):
        row = {
            "id": f"m-{len(inserted) + 1}",
            "role": body["role"],
            "content": body["content"],
            "tool_calls": body.get("tool_calls"),
        }
        inserted.append(row)
        existing.append(row)
        return row["id"]

    monkeypatch.setattr(bot_nudges, "create_message", _create)
    monkeypatch.setattr(bot_nudges, "list_messages", lambda uid, limit=60: list(existing))
    return {"inserted": inserted, "existing": existing}


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


# ---------------------------------------------------------------------------
# new_match nudges
# ---------------------------------------------------------------------------


def test_fresh_discover_card_produces_a_new_match_nudge(monkeypatch, stub_inserts):
    """A card newer than the freshness gate lands as an assistant
    message with kind=nudge and category=new_match."""
    monkeypatch.setattr(
        discover_module, "list_cards",
        lambda **kw: [
            {
                "card_kind": "opportunity",
                "card_id": "op-123",
                "title": "Chobani UGC",
                "subtitle": "$850, deadline friday",
                "created_at": _iso(datetime.now(UTC) - timedelta(hours=2)),
            }
        ],
    )
    monkeypatch.setattr(bookings_module, "list_for_user", lambda uid, **kw: [])

    ids = bot_nudges.generate_pending("u-1")

    assert len(ids) == 1
    msg = stub_inserts["inserted"][0]
    assert msg["role"] == "assistant"
    assert "Chobani UGC" in msg["content"]
    tc = msg["tool_calls"]
    assert tc["kind"] == "nudge"
    assert tc["nudge_category"] == "new_match"
    assert tc["nudge_key"] == "new_match:opportunity:op-123"
    # Chips must be manager-shaped: exactly one primary + at least one escape.
    chips = tc["chips"]
    assert isinstance(chips, list) and len(chips) >= 2
    assert sum(1 for c in chips if c.get("primary")) == 1
    labels = [c["label"] for c in chips]
    assert "pitch it" in labels
    # The third chip is the affirmative "connect" (replaces the earlier
    # dismissive "skip") so every nudge ends with two positive moves.
    assert "connect" in labels
    assert "skip" not in labels


def test_stale_discover_card_does_not_nudge(monkeypatch, stub_inserts):
    """Anything older than the freshness window is ignored — a stale
    match nudge would read as spam."""
    monkeypatch.setattr(
        discover_module, "list_cards",
        lambda **kw: [
            {
                "card_kind": "opportunity",
                "card_id": "op-999",
                "title": "Old Brief",
                "created_at": _iso(datetime.now(UTC) - timedelta(days=30)),
            }
        ],
    )
    monkeypatch.setattr(bookings_module, "list_for_user", lambda uid, **kw: [])

    ids = bot_nudges.generate_pending("u-1")
    assert ids == []
    assert stub_inserts["inserted"] == []


def test_new_match_nudge_deduped_on_second_call(monkeypatch, stub_inserts):
    """Calling generate_pending twice with the same underlying card
    inserts once and only once — the dedupe cursor is the tool_calls
    nudge_key."""
    monkeypatch.setattr(
        discover_module, "list_cards",
        lambda **kw: [
            {
                "card_kind": "brand",
                "card_id": "b-77",
                "title": "Olipop",
                "created_at": _iso(datetime.now(UTC) - timedelta(hours=1)),
            }
        ],
    )
    monkeypatch.setattr(bookings_module, "list_for_user", lambda uid, **kw: [])

    first = bot_nudges.generate_pending("u-1")
    second = bot_nudges.generate_pending("u-1")

    assert len(first) == 1
    assert second == []
    assert len(stub_inserts["inserted"]) == 1


# ---------------------------------------------------------------------------
# booking_pending nudges
# ---------------------------------------------------------------------------


def test_pending_booking_inside_horizon_nudges(monkeypatch, stub_inserts):
    monkeypatch.setattr(discover_module, "list_cards", lambda **kw: [])
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: [
            {
                "id": "bk-1",
                "title": "Olipop Shoot",
                "status": "pending",
                "starts_at": _iso(datetime.now(UTC) + timedelta(hours=24)),
            }
        ],
    )

    ids = bot_nudges.generate_pending("u-1")
    assert len(ids) == 1
    msg = stub_inserts["inserted"][0]
    tc = msg["tool_calls"]
    assert tc["nudge_category"] == "booking_pending"
    assert tc["nudge_key"] == "booking_pending:bk-1"
    assert "Olipop Shoot" in msg["content"]
    # The lock-in chip is primary, the calendar shortcut is a nav chip.
    kinds = [c["kind"] for c in tc["chips"]]
    assert "fill" in kinds and "nav" in kinds


def test_confirmed_booking_does_not_nudge(monkeypatch, stub_inserts):
    monkeypatch.setattr(discover_module, "list_cards", lambda **kw: [])
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: [
            {
                "id": "bk-2",
                "title": "Already-locked shoot",
                "status": "confirmed",
                "starts_at": _iso(datetime.now(UTC) + timedelta(hours=24)),
            }
        ],
    )
    assert bot_nudges.generate_pending("u-1") == []


def test_pending_booking_far_out_does_not_nudge(monkeypatch, stub_inserts):
    """Anything beyond the 48h horizon is too early to pester the user."""
    monkeypatch.setattr(discover_module, "list_cards", lambda **kw: [])
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: [
            {
                "id": "bk-3",
                "title": "Next week's thing",
                "status": "pending",
                "starts_at": _iso(datetime.now(UTC) + timedelta(days=7)),
            }
        ],
    )
    assert bot_nudges.generate_pending("u-1") == []


# ---------------------------------------------------------------------------
# safety — a broken source can't blank the whole batch
# ---------------------------------------------------------------------------


def test_discover_failure_still_lets_booking_nudge_fire(monkeypatch, stub_inserts):
    def _boom(**kw):
        raise RuntimeError("discover down")

    monkeypatch.setattr(discover_module, "list_cards", _boom)
    monkeypatch.setattr(
        bookings_module, "list_for_user",
        lambda uid, **kw: [
            {
                "id": "bk-9",
                "title": "Survivor",
                "status": "pending",
                "starts_at": _iso(datetime.now(UTC) + timedelta(hours=6)),
            }
        ],
    )
    ids = bot_nudges.generate_pending("u-1")
    assert len(ids) == 1
    assert "Survivor" in stub_inserts["inserted"][0]["content"]


def test_history_lookup_failure_does_not_crash(monkeypatch, stub_inserts):
    def _boom(uid, limit=60):
        raise RuntimeError("db down")

    monkeypatch.setattr(bot_nudges, "list_messages", _boom)
    monkeypatch.setattr(discover_module, "list_cards", lambda **kw: [])
    monkeypatch.setattr(bookings_module, "list_for_user", lambda uid, **kw: [])

    assert bot_nudges.generate_pending("u-1") == []


def test_bot_module_export_smoketest():
    """The nudge service imports from app.services.bot at the top of the
    module. If bot's public API breaks (rename of create_message /
    list_messages), this test fails clearly."""
    assert callable(bot_module.create_message)
    assert callable(bot_module.list_messages)


# ---------------------------------------------------------------------------
# Phase 8: deal-derived nudge sources. Every candidate must dedupe by
# nudge_key so the same event never surfaces twice, including across
# repeat visits within the same day.
# ---------------------------------------------------------------------------


def _silence_other_sources(monkeypatch):
    """Zero out sources we're not exercising so a test's assertion is
    exact. Phase 5/6/7 helpers each read their own tables; without a
    fake supabase behind them they'd swallow the exception and return
    [] anyway, but explicit is calmer."""
    for name in (
        "_match_nudges",
        "_booking_nudges",
        "_connection_accepted_nudges",
        "_event_soon_nudges",
        "_pending_action_nudges",
        "_hot_drop_nudges",
    ):
        monkeypatch.setattr(bot_nudges, name, lambda _uid: [])


def test_ghosted_deal_surfaces_a_nudge(monkeypatch, stub_inserts) -> None:
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_memory, "list_drafts", lambda _uid, **kw: []
    )
    monkeypatch.setattr(
        bot_nudges.babyg_deals,
        "list_deals",
        lambda _uid, stage=None, **kw: (
            [{"id": "d1", "brand_name": "Vans", "stage": "stale_or_ghosted"}]
            if stage == "stale_or_ghosted"
            else []
        ),
    )
    inserted = bot_nudges.generate_pending("u-1")
    assert len(inserted) == 1
    msg = stub_inserts["inserted"][0]
    assert msg["tool_calls"]["kind"] == "nudge"
    assert msg["tool_calls"]["nudge_category"] == "deal_ghosted"
    assert "Vans" in msg["content"]
    # Chips include a draft-follow-up primary.
    chips = msg["tool_calls"]["chips"]
    assert chips[0]["primary"] is True
    assert "follow up" in chips[0]["label"]


def test_ghosted_deal_dedupes_within_same_day(monkeypatch, stub_inserts) -> None:
    """Second call in the same day surfaces zero new nudges — the same
    deal on the same day maps to the same nudge_key."""
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_memory, "list_drafts", lambda _uid, **kw: []
    )
    monkeypatch.setattr(
        bot_nudges.babyg_deals,
        "list_deals",
        lambda _uid, stage=None, **kw: (
            [{"id": "d1", "brand_name": "Vans", "stage": "stale_or_ghosted"}]
            if stage == "stale_or_ghosted"
            else []
        ),
    )
    first = bot_nudges.generate_pending("u-1")
    second = bot_nudges.generate_pending("u-1")
    assert len(first) == 1
    assert len(second) == 0


def test_late_payment_nudge_reports_amount(monkeypatch, stub_inserts) -> None:
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_memory, "list_drafts", lambda _uid, **kw: []
    )
    monkeypatch.setattr(
        bot_nudges.babyg_deals,
        "list_deals",
        lambda _uid, stage=None, **kw: (
            [
                {
                    "id": "d2",
                    "brand_name": "Olipop",
                    "stage": "payment_pending",
                    "agreed_amount_cents": 200000,
                    "paid_amount_cents": None,
                }
            ]
            if stage == "payment_pending"
            else []
        ),
    )
    bot_nudges.generate_pending("u-1")
    msg = stub_inserts["inserted"][0]
    assert msg["tool_calls"]["nudge_category"] == "deal_payment_pending"
    # Amount in dollars appears in the content so the creator sees the
    # stakes without opening the deal.
    assert "$2,000" in msg["content"]
    assert "Olipop" in msg["content"]


def test_late_payment_nudge_skips_fully_paid_deals(monkeypatch, stub_inserts) -> None:
    """If paid_amount already covers agreed_amount, don't nag; the
    stage flip just hasn't caught up yet."""
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_memory, "list_drafts", lambda _uid, **kw: []
    )
    monkeypatch.setattr(
        bot_nudges.babyg_deals,
        "list_deals",
        lambda _uid, stage=None, **kw: (
            [
                {
                    "id": "d3",
                    "brand_name": "Olipop",
                    "stage": "payment_pending",
                    "agreed_amount_cents": 200000,
                    "paid_amount_cents": 200000,
                }
            ]
            if stage == "payment_pending"
            else []
        ),
    )
    inserted = bot_nudges.generate_pending("u-1")
    assert inserted == []


def test_stale_draft_surfaces_a_nudge(monkeypatch, stub_inserts) -> None:
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_deals, "list_deals", lambda _uid, **kw: []
    )
    monkeypatch.setattr(
        bot_nudges.babyg_memory,
        "list_drafts",
        lambda _uid, **kw: [
            {
                "id": "dr1",
                "subject": "re: collab",
                "to_addr": "team@vans.example",
                "status": "stale",
            }
        ],
    )
    inserted = bot_nudges.generate_pending("u-1")
    assert len(inserted) == 1
    msg = stub_inserts["inserted"][0]
    assert msg["tool_calls"]["nudge_category"] == "draft_stale"
    assert "re: collab" in msg["content"]
    chips = msg["tool_calls"]["chips"]
    assert any("discard" in c["label"] for c in chips)


def test_no_duplicate_across_repeat_visits(monkeypatch, stub_inserts) -> None:
    """Phase 8 requirement: no duplicate nudges across surfaces for
    the same underlying event. A repeat call to generate_pending must
    surface zero new items when the previous run already inserted the
    nudge."""
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_memory,
        "list_drafts",
        lambda _uid, **kw: [{"id": "dr1", "subject": "hi", "to_addr": "a@b"}],
    )
    monkeypatch.setattr(
        bot_nudges.babyg_deals,
        "list_deals",
        lambda _uid, stage=None, **kw: (
            [{"id": "dg1", "brand_name": "Vans", "stage": "stale_or_ghosted"}]
            if stage == "stale_or_ghosted"
            else (
                [
                    {
                        "id": "dp1",
                        "brand_name": "Olipop",
                        "stage": "payment_pending",
                        "agreed_amount_cents": 100000,
                    }
                ]
                if stage == "payment_pending"
                else []
            )
        ),
    )
    first = bot_nudges.generate_pending("u-1")
    second = bot_nudges.generate_pending("u-1")
    assert len(first) == 3
    # Same-day repeat visit surfaces nothing new.
    assert len(second) == 0


def test_deal_nudges_capped_per_turn(monkeypatch, stub_inserts) -> None:
    """A creator with a big pipeline doesn't get flooded on one turn.
    The cap is _MAX_DEAL_NUDGES; anything past it waits for the next
    visit. Given the daily dedupe key, that overflow surfaces tomorrow."""
    _silence_other_sources(monkeypatch)
    monkeypatch.setattr(
        bot_nudges.babyg_memory, "list_drafts", lambda _uid, **kw: []
    )
    many_ghosted = [
        {"id": f"d{i}", "brand_name": f"Brand{i}", "stage": "stale_or_ghosted"}
        for i in range(10)
    ]
    monkeypatch.setattr(
        bot_nudges.babyg_deals,
        "list_deals",
        lambda _uid, stage=None, **kw: (
            many_ghosted if stage == "stale_or_ghosted" else []
        ),
    )
    inserted = bot_nudges.generate_pending("u-1")
    assert len(inserted) == bot_nudges._MAX_DEAL_NUDGES
