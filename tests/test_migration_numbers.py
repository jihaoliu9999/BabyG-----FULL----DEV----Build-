"""Tests for scripts/check_migration_numbers.py.

The script is intentionally tiny but it gates a class of drift that has
already burned us twice. Lock the behavior down so a future tweak can't
silently regress the duplicate-prefix detection.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "check_migration_numbers.py"


def _load(tmp_migrations_dir: Path):
    """Load the script with MIGRATIONS_DIR pointed at a tmp dir."""
    spec = importlib.util.spec_from_file_location(
        "check_migration_numbers", SCRIPT
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_migration_numbers"] = module
    spec.loader.exec_module(module)
    module.MIGRATIONS_DIR = tmp_migrations_dir
    return module


def _touch(d: Path, *names: str) -> None:
    for n in names:
        (d / n).write_text("-- test fixture\n")


def test_clean_sequence_passes(tmp_path: Path) -> None:
    _touch(
        tmp_path,
        "0001_init.sql",
        "0002_users.sql",
        "0003_profiles.sql",
    )
    assert _load(tmp_path).main() == 0


def test_duplicate_prefix_fails(tmp_path: Path, capsys) -> None:
    _touch(tmp_path, "0005_alpha.sql", "0005_beta.sql")
    assert _load(tmp_path).main() == 1
    out = capsys.readouterr().out
    assert "0005" in out
    assert "0005_alpha.sql" in out
    assert "0005_beta.sql" in out


def test_allow_listed_legacy_duplicate_passes(tmp_path: Path) -> None:
    """The historical 0013 duplicate is grandfathered. Re-adding new
    duplicates is still rejected — see the next test."""
    _touch(
        tmp_path,
        "0013_creator_discovery_actions.sql",
        "0013_creator_location_fields.sql",
        "0014_next.sql",
    )
    assert _load(tmp_path).main() == 0


def test_new_duplicate_not_in_allow_list_still_fails(
    tmp_path: Path, capsys
) -> None:
    _touch(
        tmp_path,
        "0013_creator_discovery_actions.sql",
        "0013_creator_location_fields.sql",   # grandfathered
        "0042_alpha.sql",
        "0042_beta.sql",                       # NEW collision, must fail
    )
    assert _load(tmp_path).main() == 1
    out = capsys.readouterr().out
    assert "0042" in out
    # The grandfathered 0013 must NOT be in the failure output.
    assert "0013" not in out


@pytest.mark.parametrize(
    "bad_name",
    [
        "001_short.sql",          # only 3 digits
        "00001_long.sql",         # 5 digits
        "0001-dash.sql",          # dash separator
        "0001_UPPER.sql",         # uppercase
        "0001_trailing_.sql",     # trailing underscore
        "no_number_at_all.sql",
        "0001_double__under.sql", # double underscore
    ],
)
def test_bad_filename_shape_fails(
    tmp_path: Path, bad_name: str, capsys
) -> None:
    _touch(tmp_path, bad_name)
    assert _load(tmp_path).main() == 1
    out = capsys.readouterr().out
    assert bad_name in out


def test_missing_directory_fails_loudly(tmp_path: Path, capsys) -> None:
    bogus = tmp_path / "does-not-exist"
    assert _load(bogus).main() == 1
    out = capsys.readouterr().out
    assert "no migrations/" in out


def test_real_repo_state_is_clean() -> None:
    """The current repo must pass — if it doesn't, fix the repo, don't
    relax the rules."""
    import subprocess

    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, (
        f"check_migration_numbers failed on the real repo:\n{result.stdout}\n{result.stderr}"
    )
