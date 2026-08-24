"""Bot chat empty-state chip prompts — evergreen chips always show;
context chips only appear when there's real data behind them."""

from __future__ import annotations

from app.services.bot_prompts import compute_prompts


def test_no_context_returns_two_evergreens() -> None:
    prompts = compute_prompts()
    assert len(prompts) == 2
    texts = [p["text"] for p in prompts]
    assert "what needs me today?" in texts
    assert "check my week" in texts


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
    for empty in (None, "", "   "):
        prompts = compute_prompts(recent_dm_peer_name=empty)
        assert not any(p["icon"] == "pencil" for p in prompts)


def test_all_context_present_returns_four_chips_max() -> None:
    prompts = compute_prompts(
        unread_dms_count=3,
        recent_dm_peer_name="Zoe Eschman",
    )
    assert len(prompts) == 4
    assert prompts[0]["text"].startswith("summarize my 3")
    assert prompts[1]["text"] == "draft a follow-up to zoe"
    assert prompts[2]["text"] == "what needs me today?"
    assert prompts[3]["text"] == "check my week"


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
    assert "what needs me today?" in texts
    assert "check my week" in texts


def test_empty_snapshot_falls_back_to_evergreens() -> None:
    prompts = compute_prompts(snapshot=_snapshot())
    assert [p["text"] for p in prompts] == [
        "what needs me today?",
        "check my week",
    ]
