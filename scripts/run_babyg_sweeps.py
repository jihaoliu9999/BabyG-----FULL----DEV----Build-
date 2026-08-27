#!/usr/bin/env python3
"""Entrypoint for babyg background sweeps.

Called by Railway cron on whatever cadence is configured (hourly is a
reasonable default; the sweeps are idempotent so more frequent runs
just no-op faster).

Usage:
    python scripts/run_babyg_sweeps.py

Exits 0 if every sweep ran without an unhandled exception. Individual
per-item failures are logged to bot_job_failures, not bubbled up as
process exit codes — a single bad draft should not fail the whole
cron slot. Exits 1 only when the runner itself crashes (import error,
missing service credentials, etc.).
"""

from __future__ import annotations

import json
import logging
import sys

from app.services import bot_jobs

logger = logging.getLogger("run_babyg_sweeps")


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def main() -> int:
    _configure_logging()
    try:
        reports = bot_jobs.run_all()
    except Exception:
        logger.exception("run_babyg_sweeps.crashed")
        return 1
    for report in reports:
        print(
            json.dumps(
                {
                    "job": report.job_name,
                    "scanned": report.scanned,
                    "changed": report.changed,
                    "skipped_already_ran": report.skipped_already_ran,
                    "failed": report.failed,
                }
            ),
            flush=True,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
