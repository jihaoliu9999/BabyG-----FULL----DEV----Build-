"""Sentry SDK initialization for babyg.

Called once from `app.main.create_app()` before FastAPI is instantiated
so the SDK can install its FastAPI + Starlette + logging integrations
against the running app. Also imported by scripts/run_babyg_sweeps.py
so the cron sweep's per-item failures land in the same Sentry project
as web errors.

Design notes:

- **Empty DSN = no-op.** Dev boots with no Sentry env vars at all;
  configure_sentry() short-circuits, capture_exception() becomes a
  best-effort logger.exception. This keeps the boot path honest: a
  missing DSN never fails the app, and a broken Sentry setup never
  fails a background sweep.

- **Sample rates default to 0.** Only unhandled exceptions and
  events we send explicitly go up until an operator opts into
  spans / profiles via SENTRY_TRACES_SAMPLE_RATE / SENTRY_PROFILES_SAMPLE_RATE.
  Errors are free; spans are billed per unit, and we should turn
  them on with intent.

- **PII off by default.** SENTRY_SEND_DEFAULT_PII must be explicitly
  set to enable request bodies / headers / cookies in events.
  A before_send hook additionally strips the session cookie and
  Authorization header on the way out, so even if PII gets enabled
  later, the two most sensitive values never ship.

- **Noise filter.** GET /healthz and /favicon.ico transactions and
  their errors are dropped before send; they only exist for infra
  probes.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_SENSITIVE_HEADERS = {"authorization", "cookie", "x-session"}
_SENSITIVE_COOKIES = {"session"}
_HEALTHCHECK_PATHS = ("/healthz", "/favicon.ico", "/robots.txt")

_configured = False


def is_configured() -> bool:
    """True when configure_sentry successfully wired the SDK this process."""
    return _configured


def _scrub_event(event: dict[str, Any], hint: dict[str, Any] | None) -> dict[str, Any] | None:
    """Strip session cookie + auth header from every event we ship.

    Runs even when send_default_pii is off, because the FastAPI
    integration can still attach request context that includes these.
    Returning None drops the event entirely — used to filter health
    checks so they don't burn Sentry quota.
    """
    try:
        req = event.get("request") or {}
        url = str(req.get("url") or "")
        for path in _HEALTHCHECK_PATHS:
            if url.endswith(path) or path in url:
                return None
        headers = req.get("headers")
        if isinstance(headers, dict):
            for name in list(headers):
                if name.lower() in _SENSITIVE_HEADERS:
                    headers[name] = "[filtered]"
        cookies = req.get("cookies")
        if isinstance(cookies, dict):
            for name in list(cookies):
                if name.lower() in _SENSITIVE_COOKIES:
                    cookies[name] = "[filtered]"
    except Exception:
        # A scrubber that raises would silently drop useful events.
        # Log and let the (possibly slightly PII-tainted) event through
        # — the SDK's own default scrubbers still apply.
        logger.exception("sentry.scrub_event.failed")
    return event


def configure_sentry(settings) -> bool:
    """Install the Sentry SDK once per process. Returns True if installed.

    Idempotent: a second call in the same process is a no-op after the
    first success. Never raises — a bad DSN or missing sentry_sdk
    package logs and returns False so the app boots regardless.
    """
    global _configured
    if _configured:
        return True
    dsn = (getattr(settings, "sentry_dsn", "") or "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.logging import LoggingIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        logger.warning(
            "sentry.configure.skipped reason=sentry_sdk_missing "
            "install=sentry-sdk[fastapi]"
        )
        return False

    try:
        sentry_sdk.init(
            dsn=dsn,
            environment=getattr(settings, "env", "unknown"),
            release=getattr(settings, "release", None),
            traces_sample_rate=float(
                getattr(settings, "sentry_traces_sample_rate", 0.0) or 0.0
            ),
            profiles_sample_rate=float(
                getattr(settings, "sentry_profiles_sample_rate", 0.0) or 0.0
            ),
            send_default_pii=bool(
                getattr(settings, "sentry_send_default_pii", False)
            ),
            integrations=[
                FastApiIntegration(),
                StarletteIntegration(),
                LoggingIntegration(
                    level=logging.INFO,
                    event_level=logging.ERROR,
                ),
            ],
            before_send=_scrub_event,
            before_send_transaction=_scrub_event,
        )
    except Exception:
        logger.exception("sentry.configure.failed")
        return False
    _configured = True
    logger.info(
        "sentry.configure.ok env=%s traces=%s",
        getattr(settings, "env", "unknown"),
        getattr(settings, "sentry_traces_sample_rate", 0.0),
    )
    return True


def capture_exception(exc: BaseException, **tags: str) -> None:
    """Ship one exception to Sentry with optional tags. Falls back to log."""
    if not _configured:
        logger.exception("sentry.capture.skipped tags=%s", tags)
        return
    try:
        import sentry_sdk

        with sentry_sdk.push_scope() as scope:
            for k, v in tags.items():
                scope.set_tag(k, str(v))
            sentry_sdk.capture_exception(exc)
    except Exception:
        logger.exception("sentry.capture.failed tags=%s", tags)
