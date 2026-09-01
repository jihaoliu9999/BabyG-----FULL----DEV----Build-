"""Bot chat composer chip prompts — v3.

On a cold open the strip fills with 4 rotating manager questions
from _ROTATING_PROMPTS so the creator never sees the same two chips
on every visit. Live signals (unread DMs, pending action, brand
mentioned in the last assistant turn) still take priority; the
rotating pool only backfills the empty slots."""

from __future__ import annotations

import pytest

from app.services import bot_prompts as bp
from app.services.bot_prompts import compute_prompts


@pytest.fixture(autouse=True)
def _stable_hour_offset(monkeypatch):
    """Pin the rotation offset to 0 so cold-open tests are deterministic.
    The rotation itself is exercised in its own test below."""
    monkeypatch.setattr(bp, "_hour_offset", lambda: 0)


def test_no_context_returns_four_rotating_chips() -> None:
    prompts = compute_prompts()
    assert len(prompts) == 4
    texts = [p["text"] for p in prompts]
    # With the fixture pinning offset=0 the first four pool entries land.
    assert texts == [p["text"] for p in bp._ROTATING_PROMPTS[:4]]


def test_unread_dms_singular_grammar() -> None:
    prompts = compute_prompts(unread_dms_count=1)
    assert prompts[0]["text"] == "summarize my 1 unread dm"
    assert prompts[0]["icon"] == "message"


def test_unread_dms_plural_grammar() -> None:
    prompts = compute_prompts(unread_dms_count=7)
    assert prompts[0]["text"] == "summarize my 7 unread dms"


def test_zero_unread_dms_skips_the_chip() -> None:
    prompts = compute_prompts(unread_dms_count=0)
    assert not any("unread" in p["text"] for p in prompts)


def test_recent_peer_uses_first_name_lowercased() -> None:
    prompts = compute_prompts(recent_dm_peer_name="Jihao Liu")
    followup = next(p for p in prompts if p["icon"] == "pencil")
    assert followup["text"] == "draft a follow-up to jihao"


def test_missing_or_blank_peer_skips_the_chip() -> None:
    """No peer name -> no "draft a follow-up to <peer>" chip. Other
    pencil-icon chips from the rotating pool (drafts, content) are
    still fair game."""
    for empty in (None, "", "   "):
        prompts = compute_prompts(recent_dm_peer_name=empty)
        assert not any(
            p["text"].startswith("draft a follow-up to") for p in prompts
        )


def test_all_context_present_returns_four_chips_max() -> None:
    prompts = compute_prompts(
        unread_dms_count=3,
        recent_dm_peer_name="Zoe Eschman",
    )
    assert len(prompts) == 4
    # First two slots are the concrete signals; the last two backfill
    # from the rotating pool (offset pinned to 0 in the fixture).
    assert prompts[0]["text"].startswith("summarize my 3")
    assert prompts[1]["text"] == "draft a follow-up to zoe"
    assert prompts[2]["text"] == bp._ROTATING_PROMPTS[0]["text"]
    assert prompts[3]["text"] == bp._ROTATING_PROMPTS[1]["text"]


def test_prompts_never_exceed_max_four() -> None:
    # Same call; even with future context sources added, the cap holds.
    prompts = compute_prompts(unread_dms_count=99, recent_dm_peer_name="Anna")
    assert len(prompts) <= 4


def test_every_prompt_has_a_known_icon_id() -> None:
    """Icon ids must match the switch in _partials/bot_prompt_chips.html."""
    known = {"message", "pencil", "clock", "calendar"}
    for prompts in [
        compute_prompts(),
        compute_prompts(unread_dms_count=2),
        compute_prompts(recent_dm_peer_name="Anna Doe"),
        compute_prompts(unread_dms_count=2, recent_dm_peer_name="Anna Doe"),
    ]:
        for p in prompts:
            assert p["icon"] in known, f"unknown icon: {p['icon']}"


# ---------------------------------------------------------------------------
# Awareness snapshot integration (context-driven chips)
# ---------------------------------------------------------------------------


def _snapshot(**overrides):
    """Empty snapshot skeleton; tests override just the signals they care about."""
    base = {
        "unread_dms": {"count": 0, "latest_peer_name": None},
        "recent_connection_accepted": None,
        "recent_incoming_connection": None,
        "next_booking": None,
        "pending_booking": None,
        "fresh_discover_match": None,
        "pending_action_proposal": None,
        "recent_hot_drop": None,
        "open_deal_stage": None,
    }
    base.update(overrides)
    return base


def test_snapshot_accepted_connection_surfaces_say_hi_chip() -> None:
    snap = _snapshot(
        recent_connection_accepted={"peer_id": "p1", "peer_name": "Maya Chen"}
    )
    prompts = compute_prompts(snapshot=snap)
    texts = [p["text"] for p in prompts]
    assert "say hi to maya" in texts


def test_snapshot_booking_soon_surfaces_check_in_chip() -> None:
    snap = _snapshot(
        next_booking={
            "id": "b-1",
            "title": "Studio House shoot",
            "starts_at": "2026-06-19T15:00:00Z",
            "minutes_until_start": 40,
            "venue_name": "Silverlake",
        }
    )
    prompts = compute_prompts(snapshot=snap)
    texts = [p["text"] for p in prompts]
    assert any(t.startswith("send a check-in for studio house shoot") for t in texts)


def test_snapshot_pending_action_surfaces_confirm_chip() -> None:
    snap = _snapshot(
        pending_action_proposal={
            "id": "ap-1",
            "action_type": "gmail.send_email",
            "action_category": "external_write",
        }
    )
    prompts = compute_prompts(snapshot=snap)
    texts = [p["text"] for p in prompts]
    assert "open the action i still need to confirm" in texts


def test_snapshot_never_produces_more_than_four_chips() -> None:
    """Every signal populated at once. Strip still caps at 4."""
    snap = _snapshot(
        unread_dms={"count": 3, "latest_peer_name": "Garrett Reynolds"},
        recent_connection_accepted={"peer_id": "p1", "peer_name": "Maya Chen"},
        next_booking={
            "id": "b-1",
            "title": "shoot",
            "starts_at": "2026-06-19T15:00:00Z",
            "minutes_until_start": 20,
            "venue_name": "Silverlake",
        },
        pending_action_proposal={
            "id": "ap-1",
            "action_type": "gmail.send_email",
            "action_category": "external_write",
        },
        fresh_discover_match={
            "card_id": "c1",
            "card_kind": "brand",
            "title": "Beam Beauty",
        },
        recent_hot_drop={"id": "h-1", "title": "Miami rooftop pop-up"},
    )
    prompts = compute_prompts(snapshot=snap)
    assert len(prompts) == 4


def test_snapshot_dedupes_by_text() -> None:
    """Same peer surfaces as both accepted connection AND latest DM — only one chip renders."""
    snap = _snapshot(
        recent_connection_accepted={"peer_id": "p1", "peer_name": "Maya Chen"},
        unread_dms={"count": 1, "latest_peer_name": "Maya Chen"},
    )
    prompts = compute_prompts(snapshot=snap)
    texts = [p["text"] for p in prompts]
    # "say hi to maya" is present exactly once; no "draft a follow-up to maya"
    # duplicate because the accepted-peer branch takes precedence.
    assert texts.count("say hi to maya") == 1
    assert "draft a follow-up to maya" not in texts


def test_legacy_kwargs_still_work_without_snapshot() -> None:
    """Existing call-sites that pass only unread_dms_count + peer_name
    stay working — the snapshot arg is optional."""
    prompts = compute_prompts(
        unread_dms_count=2, recent_dm_peer_name="Anna Reyes"
    )
    assert len(prompts) == 4
    texts = [p["text"] for p in prompts]
    assert "summarize my 2 unread dms" in texts
    assert "draft a follow-up to anna" in texts
    # Remaining two slots come from the rotating pool.
    assert texts[2] == bp._ROTATING_PROMPTS[0]["text"]
    assert texts[3] == bp._ROTATING_PROMPTS[1]["text"]


def test_empty_snapshot_falls_back_to_rotating_pool() -> None:
    prompts = compute_prompts(snapshot=_snapshot())
    # Empty snapshot + no messages = cold open; the rotating pool fills
    # all 4 slots (offset pinned to 0 by the fixture).
    assert [p["text"] for p in prompts] == [
        p["text"] for p in bp._ROTATING_PROMPTS[:4]
    ]


# ---------------------------------------------------------------------------
# Composer v2: turn-aware chip generation.
# Chips must reflect the conversation, not just the awareness snapshot.
# ---------------------------------------------------------------------------


def _msg(role, content, **tc):
    row = {"role": role, "content": content}
    if tc:
        row["tool_calls"] = tc
    return row


def test_rotating_pool_only_fires_on_cold_open() -> None:
    """Empty thread: rotating pool backfills all 4 slots.
    Thread with a real user turn: pool must NOT appear — we rely on
    live signals instead."""
    cold = compute_prompts(messages=[])
    assert [p["text"] for p in cold] == [
        p["text"] for p in bp._ROTATING_PROMPTS[:4]
    ]


def test_hour_offset_rotates_the_pool(monkeypatch) -> None:
    """Same hour: identical chip set. Different hour: rotated set.
    Guarantees the creator does not see the same 4 chips forever."""
    monkeypatch.setattr(bp, "_hour_offset", lambda: 0)
    a = [p["text"] for p in compute_prompts()]
    monkeypatch.setattr(bp, "_hour_offset", lambda: 4)
    b = [p["text"] for p in compute_prompts()]
    assert a != b
    # Rotation is a stable cyclic shift, not a random shuffle.
    pool = [p["text"] for p in bp._ROTATING_PROMPTS]
    assert b == pool[4:8]


def test_rotating_pool_suppressed_once_conversation_has_substance() -> None:
    """Once the thread carries a real user turn we rely on live
    signals only — the rotating pool must not backfill."""
    live = compute_prompts(
        messages=[
            _msg("user", "hey"),
            _msg("assistant", "morning."),
        ]
    )
    texts = [p["text"] for p in live]
    for pool_entry in bp._ROTATING_PROMPTS:
        assert pool_entry["text"] not in texts


def test_pending_action_collapses_row_to_verb_chips() -> None:
    """A pending proposed_action on the newest assistant message is
    the ONE decision in front of the creator. Chips must collapse
    to confirm / review / cancel and nothing else."""
    prompts = compute_prompts(
        snapshot=_snapshot(),  # empty snapshot; verb chips should override anyway
        messages=[
            _msg("user", "draft that vans reply"),
            _msg(
                "assistant",
                "staged the draft.",
                kind="proposed_action",
                status="pending",
                action_type="gmail.create_draft",
                payload={"to": "team@vans.example", "subject": "re: collab", "body": "hey"},
            ),
        ],
    )
    texts = [p["text"] for p in prompts]
    assert texts == [
        "looks good, send it",
        "read the draft to vans back to me",
        "cancel it",
    ]
    tones = [p["tone"] for p in prompts]
    assert tones == ["good", "warn", "primary"]
    # Only the confirm chip auto-submits; review + cancel fill first.
    assert prompts[0]["submit"] is True
    assert prompts[1].get("submit") is not True
    assert prompts[2].get("submit") is not True


def test_pending_action_ignored_if_already_resolved() -> None:
    """A confirmed / cancelled proposal isn't a decision anymore."""
    prompts = compute_prompts(
        messages=[
            _msg("user", "yes"),
            _msg(
                "assistant",
                "sent.",
                kind="proposed_action",
                status="executed",
                action_type="gmail.create_draft",
                payload={"to": "team@vans.example"},
            ),
        ]
    )
    # No pending action, no brand-bolded reply, no snapshot: strip empty.
    assert prompts == []


def test_brand_hint_from_assistant_bold_pulls_deal_chips() -> None:
    """Assistant bolded a brand name -> chips point at that brand."""
    prompts = compute_prompts(
        messages=[
            _msg("user", "what's happening with vans"),
            _msg(
                "assistant",
                "**Vans** is at negotiating. last touch was 6 days ago.",
            ),
        ]
    )
    texts = [p["text"] for p in prompts]
    assert "draft a counter to vans" in texts
    assert "pull the vans thread" in texts
    assert "remind me about vans in 2 days" in texts
    # And no evergreens under a live turn.
    assert "what needs me today?" not in texts


def test_brand_hint_uses_domain_root_of_pending_recipient() -> None:
    """team@vans.example -> vans (domain root beats local part)."""
    prompts = compute_prompts(
        messages=[
            _msg("user", "draft it"),
            _msg(
                "assistant",
                "staged.",
                kind="proposed_action",
                status="pending",
                action_type="gmail.create_draft",
                payload={"to": "team@vans.example"},
            ),
        ]
    )
    review = next(p for p in prompts if p["tone"] == "warn")
    assert review["text"] == "read the draft to vans back to me"


def test_brand_hint_falls_back_when_domain_is_generic() -> None:
    """anna@gmail.com is a personal address, not a brand. Use the local
    part as the label instead of the mail provider."""
    prompts = compute_prompts(
        messages=[
            _msg("user", "draft it"),
            _msg(
                "assistant",
                "staged.",
                kind="proposed_action",
                status="pending",
                action_type="gmail.create_draft",
                payload={"to": "anna@gmail.com"},
            ),
        ]
    )
    review = next(p for p in prompts if p["tone"] == "warn")
    assert review["text"] == "read the draft to anna back to me"


def test_helpers_do_not_crash_on_missing_fields() -> None:
    """The helper is defensive against messages missing tool_calls or
    role. Prod data is messy. Malformed inputs get treated as if
    there were no user turn (cold-open behavior), so the rotating
    pool fills all 4 slots."""
    expected = [p["text"] for p in bp._ROTATING_PROMPTS[:4]]
    got = compute_prompts(messages=[{"content": "hi"}])
    assert [p["text"] for p in got] == expected
    got = compute_prompts(messages=[{"role": "assistant"}])
    assert [p["text"] for p in got] == expected


def test_day_word_is_not_treated_as_brand() -> None:
    """Assistant mentioning 'Friday' or 'Monday' must not become a chip
    like 'draft a counter to friday'. Days are stopwords for the
    brand extractor."""
    prompts = compute_prompts(
        messages=[
            _msg("user", "any plans"),
            _msg("assistant", "Friday is open for the shoot."),
        ]
    )
    texts = [p["text"] for p in prompts]
    assert not any("friday" in t for t in texts)
