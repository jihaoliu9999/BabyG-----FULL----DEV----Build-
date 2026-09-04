"""Boot-time migration drift detector.

Why this exists
---------------
Three deploys in a row landed code that referenced schema changes which
were never actually applied to Supabase — Phase 3's privacy/babyg/deal
columns, and earlier the `action_proposals` table that the entire bot
write-gate depends on. Every time the symptom was the same: a 500 in
production the next time a creator hit the new surface. This module
turns that silent class of bug into a loud one at boot.

What it does
------------
On app startup, ``check_migration_drift()`` compares the
``migrations/*.sql`` files in this repo to the names recorded in
``supabase_migrations.schema_migrations``. It returns a result object
describing the diff. ``app/main.py`` wires it into the existing
``_assert_*`` startup pattern.

The names in the registry come back from the ``applied_migration_names``
RPC (migration 0018) — PostgREST only exposes the ``public`` schema by
default, so a SECURITY DEFINER wrapper is the cleanest path. Returning
only names keeps the surface tiny; nothing is leaked that isn't already
visible in the repo.

Two failure modes, deliberately treated differently:

  * **missing_on_supabase** — repo has a file the registry doesn't
    list. This is the dangerous case (code may reference a column /
    table that doesn't exist). Default: log a WARN per missing
    migration. When ``STRICT_MIGRATION_CHECK=1`` in the environment,
    raise ``MigrationDriftError`` so the boot fails fast.

  * **extra_in_registry** — registry lists a name the repo has no file
    for. Happens after one-off repairs / renames. Informational only.

Failures fetching the RPC (Supabase unreachable, function missing,
service-role key absent in dev) never crash the boot. The check logs
that it couldn't run and returns ``available=False``. We don't want a
flaky Supabase to take the app offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from app.core import supabase_client

logger = logging.getLogger(__name__)


class MigrationDriftError(RuntimeError):
    """Raised when ``STRICT_MIGRATION_CHECK=1`` and the repo has
    migration files that aren't registered as applied on Supabase."""


@dataclass(frozen=True)
class MigrationCheckResult:
    available: bool
    local_migrations: tuple[str, ...] = ()
    registered: tuple[str, ...] = ()
    missing_on_supabase: tuple[str, ...] = field(default_factory=tuple)
    extra_in_registry: tuple[str, ...] = field(default_factory=tuple)
    error: str | None = None

    @property
    def drift_detected(self) -> bool:
        return bool(self.missing_on_supabase)


def _local_migration_names(repo_root: Path) -> tuple[str, ...]:
    d = repo_root / "migrations"
    if not d.is_dir():
        return ()
    return tuple(sorted(p.stem for p in d.glob("*.sql")))


def _fetch_registered_names() -> tuple[str, ...]:
    """Call the ``applied_migration_names`` RPC and return the sorted
    name array. Raises on any error — caller decides whether that
    crashes the boot."""
    client = supabase_client.get_service_client()
    # supabase-py exposes RPCs via .rpc(name).execute(); the function
    # returns a text[] which PostgREST projects to a JSON array.
    result = client.rpc("applied_migration_names", {}).execute()
    data = getattr(result, "data", None)
    if isinstance(data, list):
        names = data
    elif isinstance(data, dict):
        # Some PostgREST clients wrap scalar-array returns as a single-row
        # response with a generated column name. Tolerate either shape.
        names = next(iter(data.values()), [])
    else:
        names = []
    return tuple(sorted(str(n) for n in names if isinstance(n, str)))


def check_migration_drift(
    *,
    repo_root: Path | None = None,
) -> MigrationCheckResult:
    """Compare local migration files to the Supabase registry.

    Never raises on Supabase errors — those return ``available=False``
    with the error message attached. ``MigrationDriftError`` is raised
    by ``assert_migrations_applied()`` (the boot-side wrapper),
    not here.
    """
    root = repo_root or Path(__file__).resolve().parent.parent.parent
    local = _local_migration_names(root)
    try:
        registered = _fetch_registered_names()
    except Exception as exc:
        # Intentionally broad: any Supabase/RPC/network failure must downgrade
        # to "unavailable" rather than crash boot. Operators still see the
        # warning in the log; strict mode never escalates on uncertain data.
        logger.warning("migration check unavailable: %s", exc)
        return MigrationCheckResult(
            available=False,
            local_migrations=local,
            error=str(exc),
        )
    local_set = set(local)
    reg_set = set(registered)
    return MigrationCheckResult(
        available=True,
        local_migrations=local,
        registered=registered,
        missing_on_supabase=tuple(sorted(local_set - reg_set)),
        extra_in_registry=tuple(sorted(reg_set - local_set)),
    )


def assert_migrations_applied(settings):
    """Boot-time guard. Mirrors the ``_assert_session_secret`` /
    ``_assert_app_url`` pattern in ``app/main.py``.

    Skips entirely in ``env=dev`` because contributors run without a
    Supabase service-role key. Returns ``None`` in that case.

    In strict mode (``settings.strict_migration_check`` truthy), runs
    inline and blocks boot — that's what "strict" means; a mis-deploy
    should never start serving requests. Returns ``None``.

    In non-strict mode (the default in production too), fires the RPC
    on a background thread so container boot never waits on Supabase.
    The check still logs WARN if drift is found — an operator watching
    the deploy log sees the same signal, just a few hundred ms after
    the app started accepting requests instead of during boot. Returns
    the ``threading.Thread`` so tests / debug callers can join it.
    """
    if settings.env == "dev":
        return None
    if settings.strict_migration_check:
        _run_and_report(settings)
        return None
    # Non-strict: fire-and-forget on a daemon thread so we don't couple
    # container boot to Supabase reachability. Daemon so the process
    # can still exit cleanly if the RPC never returns.
    import threading

    thread = threading.Thread(
        target=_run_and_report,
        args=(settings,),
        name="migration-check",
        daemon=True,
    )
    thread.start()
    return thread


def _run_and_report(settings) -> None:
    """The actual drift check + log/raise dance. Extracted so the
    strict path can call it inline and the non-strict path can run
    it on a background thread."""
    result = check_migration_drift()
    if not result.available:
        logger.info(
            "migration drift check skipped (%s)", result.error or "no result"
        )
        return
    if result.extra_in_registry:
        logger.info(
            "migration registry has %d entries with no local file (likely "
            "one-off repairs): %s",
            len(result.extra_in_registry),
            ", ".join(result.extra_in_registry),
        )
    if not result.missing_on_supabase:
        logger.info(
            "migration check: all %d local migrations are registered",
            len(result.local_migrations),
        )
        return
    for name in result.missing_on_supabase:
        logger.warning(
            "migration %s is in migrations/ but NOT registered on Supabase. "
            "Code that depends on it will 500 in production.",
            name,
        )
    if settings.strict_migration_check:
        raise MigrationDriftError(
            "STRICT_MIGRATION_CHECK=1 and the following migrations are "
            "missing from Supabase: "
            + ", ".join(result.missing_on_supabase)
            + ". Apply them (supabase db push, mcp apply_migration, or "
            "the dashboard SQL editor) before booting."
        )
