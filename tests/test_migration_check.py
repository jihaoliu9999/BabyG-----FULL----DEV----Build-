"""Tests for the boot-time migration drift guard.

The unit under test is ``app.core.migration_check`` plus the
``assert_migrations_applied`` boot helper. We never hit Supabase — the
service-client RPC is stubbed so tests are deterministic.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import migration_check as mc
from app.core import supabase_client


class _StubSettings:
    def __init__(
        self,
        *,
        env: str = "production",
        strict_migration_check: bool = False,
    ) -> None:
        self.env = env
        self.strict_migration_check = strict_migration_check


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stub_rpc(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """Make the service-role client return ``names`` from the RPC."""
    rpc_result = SimpleNamespace(data=list(names))

    class _Rpc:
        def execute(self):
            return rpc_result

    class _Client:
        def rpc(self, _name: str, _params: dict) -> _Rpc:
            return _Rpc()

    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _Client()
    )


def _stub_rpc_error(
    monkeypatch: pytest.MonkeyPatch, exc: Exception
) -> None:
    class _Rpc:
        def execute(self):
            raise exc

    class _Client:
        def rpc(self, _name: str, _params: dict) -> _Rpc:
            return _Rpc()

    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _Client()
    )


def _stub_local(monkeypatch: pytest.MonkeyPatch, names: list[str]) -> None:
    """Pretend the repo has exactly these migration filenames."""
    monkeypatch.setattr(
        mc, "_local_migration_names", lambda _root: tuple(sorted(names))
    )


# ---------------------------------------------------------------------------
# check_migration_drift
# ---------------------------------------------------------------------------


def test_clean_state_reports_no_drift(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_rpc(monkeypatch, ["0001_init", "0002_users"])
    _stub_local(monkeypatch, ["0001_init", "0002_users"])

    result = mc.check_migration_drift()

    assert result.available is True
    assert result.missing_on_supabase == ()
    assert result.extra_in_registry == ()
    assert result.drift_detected is False


def test_local_files_missing_from_registry_are_flagged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_rpc(monkeypatch, ["0001_init"])
    _stub_local(monkeypatch, ["0001_init", "0002_added_locally", "0003_also_new"])

    result = mc.check_migration_drift()

    assert result.available is True
    assert result.missing_on_supabase == ("0002_added_locally", "0003_also_new")
    assert result.drift_detected is True


def test_registry_extras_are_informational_not_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Registry has names the repo doesn't (e.g. one-off repair migrations).
    That's not 'drift' for our purposes — code never references a
    migration name, only the schema it produced."""
    _stub_rpc(
        monkeypatch,
        ["0001_init", "0002_users", "0012_action_proposals_repair"],
    )
    _stub_local(monkeypatch, ["0001_init", "0002_users"])

    result = mc.check_migration_drift()

    assert result.available is True
    assert result.missing_on_supabase == ()
    assert result.extra_in_registry == ("0012_action_proposals_repair",)
    assert result.drift_detected is False


def test_supabase_failure_yields_available_false_not_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A flaky Supabase or an unset service-role key must not crash
    boot. We log and move on; operators still see the result."""
    _stub_rpc_error(monkeypatch, RuntimeError("supabase unreachable"))
    _stub_local(monkeypatch, ["0001_init"])

    result = mc.check_migration_drift()

    assert result.available is False
    assert result.error and "unreachable" in result.error
    assert result.drift_detected is False  # We don't claim drift on uncertain data


def test_handles_rpc_returning_dict_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Some PostgREST clients wrap a scalar-array RPC result in a dict
    with a generated column name. Tolerate it."""
    rpc_result = SimpleNamespace(
        data={"applied_migration_names": ["0001_init", "0002_users"]}
    )

    class _Rpc:
        def execute(self):
            return rpc_result

    class _Client:
        def rpc(self, _name: str, _params: dict) -> _Rpc:
            return _Rpc()

    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _Client()
    )
    _stub_local(monkeypatch, ["0001_init", "0002_users"])

    result = mc.check_migration_drift()

    assert result.available is True
    assert result.registered == ("0001_init", "0002_users")
    assert result.drift_detected is False


def test_local_migration_names_scans_real_directory(tmp_path: Path) -> None:
    """End-to-end sanity check that ``_local_migration_names`` reads
    the migrations dir and strips the ``.sql`` suffix."""
    d = tmp_path / "migrations"
    d.mkdir()
    (d / "0001_a.sql").write_text("-- noop")
    (d / "0002_b.sql").write_text("-- noop")
    (d / "README.md").write_text("ignored")

    names = mc._local_migration_names(tmp_path)

    assert names == ("0001_a", "0002_b")


# ---------------------------------------------------------------------------
# assert_migrations_applied (boot guard)
# ---------------------------------------------------------------------------


def test_dev_env_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Contributors run without a Supabase service-role key. The guard
    must not crash or even try to call Supabase in env=dev."""
    called = {"rpc": False}

    class _Client:
        def rpc(self, *a, **kw):
            called["rpc"] = True
            raise AssertionError("should not be called in dev")

    monkeypatch.setattr(
        supabase_client, "get_service_client", lambda: _Client()
    )

    mc.assert_migrations_applied(_StubSettings(env="dev"))

    assert called["rpc"] is False


def test_warns_on_drift_but_does_not_raise_by_default(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    _stub_rpc(monkeypatch, ["0001_init"])
    _stub_local(monkeypatch, ["0001_init", "0002_missing"])

    with caplog.at_level("WARNING", logger="app.core.migration_check"):
        thread = mc.assert_migrations_applied(_StubSettings(env="production"))
        # Non-strict mode runs on a daemon thread now; wait for the
        # log to actually land before asserting on caplog.
        if thread is not None:
            thread.join(timeout=5)

    warnings = [
        r.getMessage() for r in caplog.records if r.levelname == "WARNING"
    ]
    assert any("0002_missing" in w for w in warnings)


def test_strict_mode_raises_on_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_rpc(monkeypatch, ["0001_init"])
    _stub_local(monkeypatch, ["0001_init", "0002_missing"])

    with pytest.raises(mc.MigrationDriftError) as exc:
        mc.assert_migrations_applied(
            _StubSettings(env="production", strict_migration_check=True)
        )
    assert "0002_missing" in str(exc.value)


def test_strict_mode_does_not_raise_when_clean(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_rpc(monkeypatch, ["0001_init", "0002_users"])
    _stub_local(monkeypatch, ["0001_init", "0002_users"])

    mc.assert_migrations_applied(
        _StubSettings(env="production", strict_migration_check=True)
    )


def test_strict_mode_tolerates_supabase_outage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """STRICT only escalates on confirmed drift. A Supabase outage is
    not confirmation — never take the whole app down because we can't
    reach the registry RPC."""
    _stub_rpc_error(monkeypatch, RuntimeError("supabase down"))
    _stub_local(monkeypatch, ["0001_init"])

    mc.assert_migrations_applied(
        _StubSettings(env="production", strict_migration_check=True)
    )


def test_registry_extras_are_logged_at_info_not_warning(
    monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    _stub_rpc(monkeypatch, ["0001_init", "0012_repair"])
    _stub_local(monkeypatch, ["0001_init"])

    with caplog.at_level("INFO", logger="app.core.migration_check"):
        thread = mc.assert_migrations_applied(_StubSettings(env="production"))
        if thread is not None:
            thread.join(timeout=5)

    info_msgs = [r.getMessage() for r in caplog.records if r.levelname == "INFO"]
    assert any("one-off repairs" in m for m in info_msgs)
    warn_msgs = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    assert not any("0012_repair" in w for w in warn_msgs)
