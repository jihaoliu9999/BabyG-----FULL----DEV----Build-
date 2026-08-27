"""babyg background sweeps. Phase 7 of the babyg AI v2 plan.

Cron-driven analysis that used to happen inline on page loads now runs
here. Each sweep is:

    idempotent    Running twice produces the same result. Guarded by
                  bot_job_runs (job_name, dedupe_key).
    dedupe-keyed  Same unit of work is skipped on the second attempt.
    bounded       Fan-out is capped so a bad day never turns into
                  thousands of model calls.
    safe-to-fail  An exception in one item lands in bot_job_failures
                  and the sweep moves on.

Two tables (migration 0032):

    bot_job_runs      idempotence log; permanent
    bot_job_failures  exception log; retained until we stabilize

Every sweep returns a `SweepReport` so the Railway cron entrypoint can
log outcomes for monitoring without depending on stdout formatting.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.core import supabase_client
from app.services import babyg_deals, babyg_memory

logger = logging.getLogger(__name__)

# --- Tunables --------------------------------------------------------------

# Drafts unsent + unedited for this long roll to status='stale'. Matches
# the Phase 4 draft comment: "stale (unsent + unedited for 14+ days)".
STALE_DRAFT_DAYS = 14

# Deals in a working stage with no touchpoint in this long roll to
# stage='stale_or_ghosted'. Warm (accepted / negotiating) and cold
# (inquiry / waiting_on_terms) both share this threshold today; the
# spec says warm 7d, cold 14d for follow-up NUDGES, not for stage flips.
GHOSTED_DEAL_DAYS = 14

# Working stages we sweep for the ghosted flip. Terminal stages are
# already ignored; accepted -> stale_or_ghosted is allowed by the stage
# graph. delivered / payment_pending are kept out because "the deal is
# waiting on the money" is not the same failure mode as "the brand
# went quiet".
_WORKING_STAGES: frozenset[str] = frozenset(
    {"inquiry", "negotiating", "waiting_on_terms", "accepted"}
)

# Cap the fan-out of any single sweep so a runaway migration cannot
# consume every request slot on a shared worker.
MAX_ITEMS_PER_SWEEP = 500


# --- Data types ------------------------------------------------------------


@dataclass
class SweepReport:
    """What one sweep did. Written to bot_job_runs for the sweep itself
    (as a summary row with dedupe_key=<sweep>:<yyyymmddHH>) and used
    by the cron entrypoint to log."""

    job_name: str
    scanned: int = 0
    changed: int = 0
    skipped_already_ran: int = 0
    failed: int = 0
    details: list[dict[str, Any]] = field(default_factory=list)


# --- Idempotence primitives ------------------------------------------------


def _service():
    return supabase_client.get_service_client()


def already_ran(job_name: str, dedupe_key: str) -> bool:
    """True if (job_name, dedupe_key) is in bot_job_runs. Never raises;
    on lookup failure returns False so the caller re-does the work
    rather than silently skipping it. Idempotence at the write layer
    (job outcome writes) provides the second line of defense."""
    if not job_name or not dedupe_key:
        return False
    try:
        rows = list(
            _service()
            .table("bot_job_runs")
            .select("id")
            .eq("job_name", job_name)
            .eq("dedupe_key", dedupe_key)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.info("bot_jobs.already_ran_lookup_failed", exc_info=True)
        return False
    return bool(rows)


def mark_ran(
    job_name: str,
    dedupe_key: str,
    *,
    outcome: str = "ok",
    target_user_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> bool:
    """Record that (job_name, dedupe_key) was handled. Best-effort.
    Returns True on success. `outcome` is one of ok / skipped / failed."""
    if outcome not in {"ok", "skipped", "failed"}:
        outcome = "ok"
    row: dict[str, Any] = {
        "job_name": job_name,
        "dedupe_key": dedupe_key,
        "outcome": outcome,
        "detail": detail or {},
    }
    if target_user_id:
        row["target_user_id"] = target_user_id
    try:
        _service().table("bot_job_runs").insert(row).execute()
        return True
    except Exception:
        # Unique constraint on (job_name, dedupe_key) means a second
        # writer will error — that is the point. Log at info; do not
        # raise into the sweep.
        logger.info(
            "bot_jobs.mark_ran_failed job=%s key=%s",
            job_name,
            dedupe_key,
            exc_info=True,
        )
        return False


def record_failure(
    job_name: str,
    exc: BaseException,
    *,
    dedupe_key: str | None = None,
    target_user_id: str | None = None,
) -> None:
    """Write one row to bot_job_failures. Truncates the message tail
    so a pathological exception cannot bloat the log."""
    row: dict[str, Any] = {
        "job_name": job_name,
        "exception_class": type(exc).__name__,
        "exception_message": str(exc)[:2000],
    }
    if dedupe_key:
        row["dedupe_key"] = dedupe_key
    if target_user_id:
        row["target_user_id"] = target_user_id
    try:
        _service().table("bot_job_failures").insert(row).execute()
    except Exception:
        logger.warning(
            "bot_jobs.record_failure_write_failed job=%s", job_name, exc_info=True
        )


# --- Sweep: stale drafts ---------------------------------------------------


def sweep_stale_drafts(*, now: datetime | None = None) -> SweepReport:
    """Flip drafts that have been sitting for STALE_DRAFT_DAYS to
    status='stale'. Only touches drafts still in 'proposed' or 'edited'
    — approved/sent/canceled drafts are already terminal for this
    sweep."""
    report = SweepReport(job_name="sweep_stale_drafts")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=STALE_DRAFT_DAYS)
    try:
        rows = list(
            _service()
            .table("babyg_memory_drafts")
            .select("id, creator_id, updated_at, status")
            .in_("status", ["proposed", "edited"])
            .lte("updated_at", cutoff.isoformat())
            .order("updated_at", desc=True)
            .limit(MAX_ITEMS_PER_SWEEP)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        record_failure("sweep_stale_drafts", exc)
        report.failed += 1
        return report

    for row in rows:
        report.scanned += 1
        draft_id = str(row.get("id") or "")
        creator_id = str(row.get("creator_id") or "")
        if not draft_id:
            continue
        dedupe_key = f"stale_draft:{draft_id}"
        if already_ran("sweep_stale_drafts", dedupe_key):
            report.skipped_already_ran += 1
            continue
        try:
            ok = babyg_memory.update_draft_status(draft_id, "stale")
        except Exception as exc:
            record_failure(
                "sweep_stale_drafts",
                exc,
                dedupe_key=dedupe_key,
                target_user_id=creator_id or None,
            )
            report.failed += 1
            continue
        if ok:
            mark_ran(
                "sweep_stale_drafts",
                dedupe_key,
                target_user_id=creator_id or None,
                detail={"draft_id": draft_id},
            )
            report.changed += 1
    return report


# --- Sweep: ghosted deals --------------------------------------------------


def sweep_ghosted_deals(*, now: datetime | None = None) -> SweepReport:
    """Flip working-stage deals with no touch in GHOSTED_DEAL_DAYS to
    stage='stale_or_ghosted'. Terminal stages and non-working stages
    (delivered / payment_pending) are left alone — those are waiting on
    the money, not on the brand."""
    report = SweepReport(job_name="sweep_ghosted_deals")
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(days=GHOSTED_DEAL_DAYS)
    try:
        rows = list(
            _service()
            .table("babyg_memory_deals")
            .select("id, creator_id, stage, last_touch_at")
            .in_("stage", sorted(_WORKING_STAGES))
            .lte("last_touch_at", cutoff.isoformat())
            .order("last_touch_at", desc=True)
            .limit(MAX_ITEMS_PER_SWEEP)
            .execute()
            .data
            or []
        )
    except Exception as exc:
        record_failure("sweep_ghosted_deals", exc)
        report.failed += 1
        return report

    for row in rows:
        report.scanned += 1
        deal_id = str(row.get("id") or "")
        creator_id = str(row.get("creator_id") or "")
        if not deal_id or not creator_id:
            continue
        # Dedupe by day so a deal that stays quiet does not re-fire the
        # nudge every hour, but a follow-up sweep the next day gets a
        # fresh chance if we want to escalate later.
        day = now.strftime("%Y%m%d")
        dedupe_key = f"ghosted_deal:{deal_id}:{day}"
        if already_ran("sweep_ghosted_deals", dedupe_key):
            report.skipped_already_ran += 1
            continue
        try:
            out = babyg_deals.update_stage(
                deal_id, "stale_or_ghosted", creator_id=creator_id
            )
        except Exception as exc:
            record_failure(
                "sweep_ghosted_deals",
                exc,
                dedupe_key=dedupe_key,
                target_user_id=creator_id,
            )
            report.failed += 1
            continue
        if out is not None:
            mark_ran(
                "sweep_ghosted_deals",
                dedupe_key,
                target_user_id=creator_id,
                detail={"deal_id": deal_id, "from_stage": row.get("stage")},
            )
            report.changed += 1
        else:
            # The stage graph refused (e.g. accepted->stale is fine but
            # the row moved to delivered between select and update).
            # Skip marker so we do not thrash on it.
            mark_ran(
                "sweep_ghosted_deals",
                dedupe_key,
                outcome="skipped",
                target_user_id=creator_id,
                detail={"deal_id": deal_id, "reason": "stage_refused"},
            )
    return report


# --- Sweep runner ----------------------------------------------------------


def run_all(*, now: datetime | None = None) -> list[SweepReport]:
    """Run every registered sweep in order. Returns their reports.
    Called by scripts/run_babyg_sweeps.py from Railway cron."""
    sweeps = [sweep_stale_drafts, sweep_ghosted_deals]
    reports: list[SweepReport] = []
    for sweep in sweeps:
        try:
            report = sweep(now=now)
        except Exception as exc:
            logger.exception("bot_jobs.sweep_crashed name=%s", sweep.__name__)
            record_failure(sweep.__name__, exc)
            report = SweepReport(job_name=sweep.__name__, failed=1)
        reports.append(report)
    return reports
