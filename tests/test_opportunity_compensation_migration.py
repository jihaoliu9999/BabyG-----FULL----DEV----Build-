"""Static safety contract for the narrowly scoped compensation migration."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/0044_opportunity_negotiable_compensation.sql"
CLI_MIGRATION = ROOT / (
    "supabase/migrations/"
    "20260928000000_0044_opportunity_negotiable_compensation.sql"
)


def test_compensation_migration_preserves_all_legacy_values():
    original = (ROOT / "migrations/0019_opportunity_cards.sql").read_text()
    original_check = re.search(
        r"check \(compensation_type in \((.*?)\)\)", original, re.DOTALL
    )
    assert original_check is not None
    legacy = re.findall(r"'([^']+)'", original_check.group(1))
    assert legacy == [
        "unspecified", "flat_rate", "hourly", "gifted", "trade", "revenue_share"
    ]
    assert re.findall(r"'([^']+)'", MIGRATION.read_text()) == [
        *legacy, "negotiable"
    ]


def test_compensation_migration_only_replaces_named_check_atomically():
    sql = re.sub(r"--[^\n]*", "", MIGRATION.read_text())
    statements = [" ".join(s.split()) for s in sql.split(";") if s.strip()]
    assert statements == [
        "begin",
        "alter table public.creator_job_listings "
        "drop constraint creator_job_listings_compensation_type_check",
        "alter table public.creator_job_listings "
        "add constraint creator_job_listings_compensation_type_check "
        "check (compensation_type in ( 'unspecified', 'flat_rate', 'hourly', "
        "'gifted', 'trade', 'revenue_share', 'negotiable' ))",
        "commit",
    ]


def test_cli_compensation_migration_matches_root_and_has_new_version():
    assert CLI_MIGRATION.read_bytes() == MIGRATION.read_bytes()
    existing_versions = [
        path.name.split("_", 1)[0]
        for path in (ROOT / "supabase/migrations").glob("*.sql")
        if path != CLI_MIGRATION
    ]
    assert CLI_MIGRATION.name.split("_", 1)[0] > max(existing_versions)
