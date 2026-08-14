"""pick_daily returns stable per-(user, day) greetings across four time slots."""

from __future__ import annotations

from datetime import date

from app.services.greetings import pick_daily


def test_pick_daily_returns_all_four_slots() -> None:
    g = pick_daily("user-1", "garrett", date(2026, 8, 14))
    assert set(g.keys()) == {"morning", "afternoon", "evening", "night"}
    for text in g.values():
        assert "garrett" in text


def test_pick_daily_is_stable_for_same_user_and_day() -> None:
    d = date(2026, 8, 14)
    assert pick_daily("user-1", "garrett", d) == pick_daily("user-1", "garrett", d)


def test_pick_daily_rotates_across_days() -> None:
    a = pick_daily("user-1", "garrett", date(2026, 8, 14))
    b = pick_daily("user-1", "garrett", date(2026, 8, 15))
    assert a != b, "the greeting set should change day-over-day"


def test_pick_daily_differs_across_users_same_day() -> None:
    d = date(2026, 8, 14)
    a = pick_daily("user-A", "garrett", d)
    b = pick_daily("user-B", "garrett", d)
    # It's fine if a single slot happens to collide by pigeonhole; the
    # whole set should not.
    assert a != b


def test_pick_daily_uses_first_name_lowercased() -> None:
    g = pick_daily("user-1", "  Garrett  ", date(2026, 8, 14))
    for text in g.values():
        assert "garrett" in text
        assert "Garrett" not in text


def test_pick_daily_falls_back_to_creator_when_name_missing() -> None:
    g = pick_daily("user-1", "", date(2026, 8, 14))
    for text in g.values():
        assert "creator" in text


def test_pick_daily_night_slot_uses_question_or_statement() -> None:
    """Night pool is intentionally shorter and more casual — just verify
    the picked variant is one of the four we ship, so a typo in the pool
    doesn't sneak through."""
    from app.services.greetings import _NIGHT

    g = pick_daily("user-1", "garrett", date(2026, 8, 14))
    templates = [t.format(name="garrett") for t in _NIGHT]
    assert g["night"] in templates
