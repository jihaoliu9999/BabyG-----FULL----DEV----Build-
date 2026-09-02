#!/usr/bin/env python3
"""Entrypoint for babyg background sweeps.

Called by Railway cron. All five sweeps are idempotent per
(job_name, dedupe_key), so a re-run of the same slot is safe —
extra runs just no-op faster.

Usage:
    python scripts/run_babyg_sweeps.py              # run every sweep
    python scripts/run_babyg_sweeps.py --filter gmail
    python scripts/run_babyg_sweeps.py --filter ig,dm

`--filter` accepts a comma-separated list of substrings that must
match `SweepReport.job_name`. Matches are case-insensitive. Useful
for backfill runs: `--filter ig` invokes only `sweep_ig_metrics`.

Exits 0 when the runner reaches the end of its sweep list — even if
individual items inside a sweep raised, because those land in
bot_job_failures and shouldn't fail the whole slot. Exits 1 only
when the runner itself crashes (import error, missing service
credentials, config broken). This matters for Railway alerting:
green means "the cron machinery worked", not "every creator's mail
processed cleanly."

Env required:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY
    ANTHROPIC_API_KEY (Gmail sweep skips drafting without it)
    GOOGLE_CLIENT_ID + secret (Gmail sweep skips without google_calendar.is_configured)
    INSTAGRAM_CLIENT_ID + secret (IG sweep skips without instagram_meta.is_configured)
"""

from __future__ import annotations

import argparse
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run babyg background sweeps.")
    parser.add_argument(
        "--filter",
        default="",
        help=(
            "Comma-separated substring match against SweepReport.job_name "
            "(case-insensitive). Empty runs every sweep."
        ),
    )
    return parser.parse_args(argv)


def _select_sweeps(all_sweeps, filter_arg: str):
    if not filter_arg.strip():
        return all_sweeps
    needles = [p.strip().lower() for p in filter_arg.split(",") if p.strip()]
    return [s for s in all_sweeps if any(n in s.__name__.lower() for n in needles)]


def main(argv: list[str] | None = None) -> int:
    _configure_logging()
    args = _parse_args(argv)
    all_sweeps = [
        bot_jobs.sweep_stale_drafts,
        bot_jobs.sweep_ghosted_deals,
        bot_jobs.sweep_gmail_briefs,
        bot_jobs.sweep_dm_briefs,
        bot_jobs.sweep_ig_metrics,
    ]
    selected = _select_sweeps(all_sweeps, args.filter)
    if not selected:
        logger.warning("run_babyg_sweeps.no_sweep_matched filter=%s", args.filter)
        return 0
    reports = []
    for sweep in selected:
        try:
            report = sweep()
        except Exception as exc:
            logger.exception("run_babyg_sweeps.sweep_crashed name=%s", sweep.__name__)
            bot_jobs.record_failure(sweep.__name__, exc)
            report = bot_jobs.SweepReport(job_name=sweep.__name__, failed=1)
        reports.append(report)
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
    try:
        sys.exit(main())
    except Exception:
        logger.exception("run_babyg_sweeps.crashed")
        sys.exit(1)
