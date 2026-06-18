"""Static safety checks for the Phase 4 migration sequence."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "migrations"


def _sql(name: str) -> str:
    return (MIGRATIONS / name).read_text(encoding="utf-8").lower()


def test_discovery_view_is_security_invoker_and_not_browser_exposed():
    sql = _sql("0020_discovery_card_view.sql")
    assert "security_invoker = true" in sql
    assert "revoke all on public.discovery_cards from anon, authenticated" in sql
    assert "'/creator/discover/brand/'" in sql
    assert "'/creator/brands/'" not in sql
    assert "location_lat" not in sql
    assert "location_lng" not in sql
    assert "verification_notes" not in sql
    assert "location_display_level" in sql
    assert "when 'hidden' then null" in sql


def test_existing_creator_actions_are_backfilled_before_not_null():
    sql = _sql("0021_mixed_discovery_actions.sql")
    backfill = sql.index("set target_card_id = target_user_id")
    not_null = sql.index("alter column target_card_id set not null")
    assert backfill < not_null
    assert "'saved'" in sql
    assert "'interested'" in sql


def test_opportunity_budget_constraint_prevents_inverted_ranges():
    sql = _sql("0019_opportunity_cards.sql")
    assert "budget_min <= budget_max" in sql
    assert "budget_min >= 0" in sql
    assert "budget_max >= 0" in sql
