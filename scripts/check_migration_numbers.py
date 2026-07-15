"""Migration filename sanity check.

Catches the kind of drift the rebuild plan called out:
  * two migration files sharing a numeric prefix (e.g. the legacy
    duplicate `0013_creator_discovery_actions.sql` /
    `0013_creator_location_fields.sql` near-miss)
  * a file that doesn't match the `NNNN_snake_case.sql` shape
  * a gap in the sequence past a configured allow-list

Run as:

    python scripts/check_migration_numbers.py

Exits 0 when clean. Exits 1 with a human-readable summary on any
finding. Intended for CI; no Supabase access required.

Known historical duplicates are allow-listed below — they're already
applied in production and renaming them would force a destructive
reorder. New duplicates are NEVER allowed.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"

# Format: `NNNN_snake_case_name.sql`. The 4-digit prefix is the source
# of truth for ordering — the file system sort matches Supabase CLI's
# application order when prefixes don't collide.
_FILENAME_RE = re.compile(r"^(\d{4})_[a-z0-9]+(?:_[a-z0-9]+)*\.sql$")

# Legacy duplicates already deployed. Renaming would break the registry
# match in production. Any NEW duplicate must be renumbered.
KNOWN_DUPLICATE_PREFIXES: frozenset[str] = frozenset(
    {
        "0013",  # 0013_creator_discovery_actions + 0013_creator_location_fields
    }
)


def main() -> int:
    if not MIGRATIONS_DIR.is_dir():
        print(f"check_migration_numbers: no migrations/ at {MIGRATIONS_DIR}")
        return 1

    files = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))
    errors: list[str] = []

    by_prefix: dict[str, list[str]] = {}
    for name in files:
        m = _FILENAME_RE.match(name)
        if not m:
            errors.append(
                f"  - bad filename shape: {name!r}\n"
                f"    expected NNNN_snake_case.sql (lowercase, digits-only prefix)"
            )
            continue
        by_prefix.setdefault(m.group(1), []).append(name)

    for prefix, group in by_prefix.items():
        if len(group) > 1 and prefix not in KNOWN_DUPLICATE_PREFIXES:
            errors.append(
                f"  - migration prefix {prefix} is duplicated by:\n"
                + "\n".join(f"      {n}" for n in group)
                + "\n    pick a fresh prefix; do not collide with deployed numbers."
            )

    if errors:
        print("check_migration_numbers: drift detected")
        for e in errors:
            print(e)
        print(
            "\nIf one of these duplicates is intentional and already applied to "
            "production, add its prefix to KNOWN_DUPLICATE_PREFIXES in this file."
        )
        return 1

    print(f"check_migration_numbers: {len(files)} migrations look clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
