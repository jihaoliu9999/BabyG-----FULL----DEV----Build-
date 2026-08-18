"""discover_insights — the coral 'why babyg picked this' reasons and
the freshness signal badges rendered on each discovery card. Data-driven:
absence of a signal produces an absent item, never a stub."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.services.discover_insights import relevance_reasons, signal_badges

# -----------------------------------------------------------------------------
# relevance_reasons
# -----------------------------------------------------------------------------


def test_no_matches_returns_empty_list() -> None:
    card = {"card_kind": "creator", "tags": ["fashion"], "location_label": "NYC"}
    assert relevance_reasons(card, viewer_tags=["food"]) == []


def test_niche_overlap_single_hit() -> None:
    card = {"card_kind": "creator", "tags": ["food", "reels"]}
    out = relevance_reasons(card, viewer_tags=["food", "fitness"])
    assert len(out) == 1
    assert out[0]["icon"] == "target"
    assert "food" in out[0]["text"]
    assert "niches" in out[0]["text"]


def test_niche_overlap_two_hits_uses_plus() -> None:
    card = {"card_kind": "creator", "tags": ["food", "fitness", "reels"]}
    out = relevance_reasons(card, viewer_tags=["food", "fitness"])
    assert out[0]["text"] == "overlaps your food + fitness niches"


def test_niche_overlap_three_plus_hits_summarizes() -> None:
    card = {"card_kind": "creator", "tags": ["a", "b", "c", "d"]}
    out = relevance_reasons(card, viewer_tags=["a", "b", "c", "d"])
    assert "more" in out[0]["text"]


def test_tag_matching_is_case_insensitive() -> None:
    card = {"card_kind": "creator", "tags": ["Food"]}
    out = relevance_reasons(card, viewer_tags=["FOOD"])
    assert len(out) == 1


def test_location_match_on_city_level() -> None:
    card = {"card_kind": "creator", "location_label": "Miami, FL"}
    out = relevance_reasons(card, viewer_location_label="miami")
    pin = next((r for r in out if r["icon"] == "pin"), None)
    assert pin is not None
    assert "Miami, FL" in pin["text"]


def test_location_match_ignores_missing_viewer_location() -> None:
    card = {"card_kind": "creator", "location_label": "Miami, FL"}
    out = relevance_reasons(card)
    assert not any(r["icon"] == "pin" for r in out)


def test_location_no_match_produces_no_pin() -> None:
    card = {"card_kind": "creator", "location_label": "Los Angeles, CA"}
    out = relevance_reasons(card, viewer_location_label="miami, fl")
    assert not any(r["icon"] == "pin" for r in out)


def test_platform_match_on_creator_cards() -> None:
    card = {"card_kind": "creator", "primary_platform": "Instagram"}
    out = relevance_reasons(card, viewer_platform="instagram")
    assert any(r["icon"] == "platform" for r in out)


def test_platform_match_ignored_on_non_creator_cards() -> None:
    """We compare viewer's primary platform to card's platform only on
    creator cards; brands/opportunities don't have a comparable field."""
    card = {"card_kind": "brand", "primary_platform": "Instagram"}
    out = relevance_reasons(card, viewer_platform="Instagram")
    assert not any(r["icon"] == "platform" for r in out)


def test_opportunity_deadline_urgency_days() -> None:
    from datetime import datetime, timedelta

    future = (datetime.now(UTC) + timedelta(days=5)).isoformat()
    card = {"card_kind": "opportunity", "deadline": future}
    out = relevance_reasons(card)
    cal = next((r for r in out if r["icon"] == "calendar"), None)
    assert cal is not None
    assert "days" in cal["text"]


def test_opportunity_deadline_today_wording() -> None:
    card = {"card_kind": "opportunity", "deadline": datetime.now(UTC).isoformat()}
    out = relevance_reasons(card)
    cal = next((r for r in out if r["icon"] == "calendar"), None)
    assert cal is not None
    assert cal["text"] == "closes today"


def test_opportunity_deadline_far_future_produces_no_urgency() -> None:
    future = (datetime.now(UTC) + timedelta(days=90)).isoformat()
    card = {"card_kind": "opportunity", "deadline": future}
    out = relevance_reasons(card)
    assert not any(r["icon"] == "calendar" for r in out)


def test_all_signals_stack_ranked_and_capped() -> None:
    card = {
        "card_kind": "creator",
        "tags": ["food", "fitness"],
        "location_label": "Miami, FL",
        "primary_platform": "Instagram",
    }
    out = relevance_reasons(
        card,
        viewer_tags=["food", "fitness"],
        viewer_location_label="miami, fl",
        viewer_platform="Instagram",
    )
    assert len(out) == 3
    # Niche match is the strongest signal — must render first.
    assert out[0]["icon"] == "target"


# -----------------------------------------------------------------------------
# signal_badges
# -----------------------------------------------------------------------------


def test_new_to_babyg_within_window() -> None:
    now = datetime.now(UTC)
    card = {"created_at": (now - timedelta(days=5)).isoformat()}
    out = signal_badges(card, now=now)
    assert any(b["kind"] == "new" for b in out)


def test_new_to_babyg_outside_window_hidden() -> None:
    now = datetime.now(UTC)
    card = {"created_at": (now - timedelta(days=60)).isoformat()}
    out = signal_badges(card, now=now)
    assert not any(b["kind"] == "new" for b in out)


def test_active_this_week_fires_when_last_seen_recent() -> None:
    now = datetime.now(UTC)
    card = {"last_seen_at": (now - timedelta(days=2)).isoformat()}
    out = signal_badges(card, now=now)
    assert any(b["kind"] == "active" for b in out)


def test_active_this_week_hidden_when_last_seen_stale() -> None:
    now = datetime.now(UTC)
    card = {"last_seen_at": (now - timedelta(days=30)).isoformat()}
    out = signal_badges(card, now=now)
    assert not any(b["kind"] == "active" for b in out)


def test_responds_fast_fires_only_under_24h() -> None:
    assert any(
        b["kind"] == "responsive"
        for b in signal_badges({"dm_median_response_hours": 6})
    )
    assert not any(
        b["kind"] == "responsive"
        for b in signal_badges({"dm_median_response_hours": 48})
    )


def test_signals_absent_produce_no_badges() -> None:
    """Card has no created_at, no last_seen, no response aggregate —
    row renders as no badges (empty list), not stub badges."""
    assert signal_badges({}) == []


def test_malformed_timestamps_do_not_crash() -> None:
    """Bad iso strings must degrade silently — a broken row shouldn't
    500 the whole card render."""
    out = signal_badges({"created_at": "not-a-date", "last_seen_at": "also-bad"})
    assert out == []


def test_signal_badges_capped_at_three() -> None:
    now = datetime.now(UTC)
    card = {
        "created_at": (now - timedelta(days=5)).isoformat(),
        "last_seen_at": (now - timedelta(days=2)).isoformat(),
        "dm_median_response_hours": 6,
    }
    assert len(signal_badges(card, now=now)) <= 3
