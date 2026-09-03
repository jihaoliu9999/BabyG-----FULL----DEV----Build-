"""Sentry init tests.

The contract worth locking in:

- empty DSN -> no-op, no import errors, is_configured() stays False
- valid DSN -> sentry_sdk.init called once with the right options
- second call in same process is idempotent (no duplicate init)
- capture_exception with no init -> falls back to logger.exception
- _scrub_event drops healthchecks + redacts session cookie / auth header

Tests reset the module-level `_configured` flag between runs so
one test's init doesn't stick.
"""

from __future__ import annotations

import logging
import sys
from types import SimpleNamespace

import pytest

from app.core import sentry_init


@pytest.fixture(autouse=True)
def _reset_configured_flag():
    sentry_init._configured = False
    yield
    sentry_init._configured = False


def test_configure_with_empty_dsn_is_noop() -> None:
    settings = SimpleNamespace(sentry_dsn="", env="dev")
    assert sentry_init.configure_sentry(settings) is False
    assert sentry_init.is_configured() is False


def test_configure_with_valid_dsn_calls_init(monkeypatch) -> None:
    called: dict = {}

    class _FakeSdk:
        def init(self, **kwargs):
            called.update(kwargs)

        def push_scope(self):
            raise AssertionError("not used in this test")

        def capture_exception(self, exc):
            raise AssertionError("not used in this test")

    fake_sdk = _FakeSdk()
    fake_fastapi = SimpleNamespace(FastApiIntegration=lambda *a, **kw: "fastapi_int")
    fake_starlette = SimpleNamespace(
        StarletteIntegration=lambda *a, **kw: "starlette_int"
    )
    fake_logging = SimpleNamespace(
        LoggingIntegration=lambda level, event_level: "logging_int"
    )

    monkeypatch.setitem(sys.modules, "sentry_sdk", fake_sdk)
    monkeypatch.setitem(sys.modules, "sentry_sdk.integrations", SimpleNamespace())
    monkeypatch.setitem(
        sys.modules, "sentry_sdk.integrations.fastapi", fake_fastapi
    )
    monkeypatch.setitem(
        sys.modules, "sentry_sdk.integrations.starlette", fake_starlette
    )
    monkeypatch.setitem(
        sys.modules, "sentry_sdk.integrations.logging", fake_logging
    )

    settings = SimpleNamespace(
        sentry_dsn="https://public@sentry.example.com/1",
        env="production",
        sentry_traces_sample_rate=0.25,
        sentry_profiles_sample_rate=0.0,
        sentry_send_default_pii=False,
    )
    assert sentry_init.configure_sentry(settings) is True
    assert sentry_init.is_configured() is True
    assert called["dsn"] == "https://public@sentry.example.com/1"
    assert called["environment"] == "production"
    assert called["traces_sample_rate"] == 0.25
    assert called["send_default_pii"] is False
    assert callable(called["before_send"])


def test_configure_is_idempotent(monkeypatch) -> None:
    calls: list = []

    class _FakeSdk:
        def init(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setitem(sys.modules, "sentry_sdk", _FakeSdk())
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk.integrations.fastapi",
        SimpleNamespace(FastApiIntegration=lambda *a, **kw: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk.integrations.starlette",
        SimpleNamespace(StarletteIntegration=lambda *a, **kw: None),
    )
    monkeypatch.setitem(
        sys.modules,
        "sentry_sdk.integrations.logging",
        SimpleNamespace(LoggingIntegration=lambda level, event_level: None),
    )
    settings = SimpleNamespace(
        sentry_dsn="https://public@sentry.example.com/1",
        env="production",
    )
    assert sentry_init.configure_sentry(settings) is True
    assert sentry_init.configure_sentry(settings) is True
    assert len(calls) == 1


def test_configure_swallows_import_error(monkeypatch, caplog) -> None:
    monkeypatch.setitem(sys.modules, "sentry_sdk", None)
    settings = SimpleNamespace(
        sentry_dsn="https://public@sentry.example.com/1", env="production"
    )
    with caplog.at_level(logging.WARNING):
        assert sentry_init.configure_sentry(settings) is False
    assert any(
        "sentry.configure.skipped" in r.message for r in caplog.records
    )


def test_capture_exception_without_init_falls_back_to_log(caplog) -> None:
    with caplog.at_level(logging.ERROR):
        sentry_init.capture_exception(RuntimeError("boom"), job="test")
    assert any("sentry.capture.skipped" in r.message for r in caplog.records)


def test_scrub_event_drops_healthcheck() -> None:
    event = {"request": {"url": "https://babyg.ai/healthz"}}
    assert sentry_init._scrub_event(event, None) is None


def test_scrub_event_redacts_session_cookie_and_auth_header() -> None:
    event = {
        "request": {
            "url": "https://babyg.ai/creator/bot",
            "headers": {
                "Authorization": "Bearer secret",
                "X-Session": "abc",
                "Accept": "text/html",
            },
            "cookies": {"session": "eyJhbGc...", "csrf_token": "tok"},
        }
    }
    scrubbed = sentry_init._scrub_event(event, None)
    assert scrubbed is not None
    assert scrubbed["request"]["headers"]["Authorization"] == "[filtered]"
    assert scrubbed["request"]["headers"]["X-Session"] == "[filtered]"
    assert scrubbed["request"]["headers"]["Accept"] == "text/html"
    assert scrubbed["request"]["cookies"]["session"] == "[filtered]"
    assert scrubbed["request"]["cookies"]["csrf_token"] == "tok"


def test_scrub_event_never_raises_on_malformed_input() -> None:
    # A malformed event (non-dict request) must not crash the SDK's
    # before_send path. Return the event unchanged so the SDK's own
    # defaults still get a shot at scrubbing.
    event = {"request": "unexpected string"}
    result = sentry_init._scrub_event(event, None)
    assert result is event
