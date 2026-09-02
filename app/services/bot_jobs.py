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
from app.integrations import anthropic_client, google_calendar, google_gmail
from app.services import (
    action_proposals,
    babyg_deals,
    babyg_memory,
    babyg_relations,
    bot,
    oauth_connections,
    profiles,
)

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


# --- Sweep: gmail briefs ---------------------------------------------------
#
# For every creator with Gmail connected + a fresh unread inbound thread
# from a brand-ish sender, thread a touchpoint onto the matching deal,
# draft a reply via Claude, stage it as a gmail.create_draft action
# proposal, and drop a nudge into the bot thread. The creator confirms
# the action card to save the draft to their Gmail drafts folder — we
# never send. This is the "babyg is watching" behavior turned on.

# Cap fan-out per sweep so a runaway user base can't blast Claude spend.
MAX_GMAIL_CREATORS_PER_SWEEP = 100
MAX_GMAIL_DRAFTS_PER_CREATOR_PER_SWEEP = 3
GMAIL_SWEEP_THREAD_LIMIT = 8

# Personal-mail providers whose local-part is what identifies the
# sender, not the domain. Mirrors _brand_hint_from_action in
# bot_prompts.py so brand extraction is consistent across surfaces.
_PERSONAL_MAIL_DOMAINS: frozenset[str] = frozenset({
    "gmail", "outlook", "hotmail", "yahoo", "icloud",
    "proton", "protonmail", "aol", "live", "me", "msn", "mail",
})


def sweep_gmail_briefs(*, now: datetime | None = None) -> SweepReport:
    """Scan every Gmail-connected creator's recent threads and stage a
    draft reply for each fresh brand-ish inbound. Dedupe key
    `gmail_thread:<thread_id>:<latest_message_id>` guarantees the same
    message never generates two drafts."""
    report = SweepReport(job_name="sweep_gmail_briefs")
    if not google_calendar.is_configured():
        return report
    creators = oauth_connections.list_creators_with_google_scope(
        google_calendar.has_gmail_compose_scope,
        limit=MAX_GMAIL_CREATORS_PER_SWEEP,
    )
    for row in creators:
        creator_id = row["user_id"]
        if not creator_id:
            continue
        try:
            token = oauth_connections.access_token_for_google(creator_id)
        except Exception as exc:
            record_failure("sweep_gmail_briefs", exc, target_user_id=creator_id)
            report.failed += 1
            continue
        if not token:
            continue
        try:
            threads = google_gmail.list_recent_threads(
                token, limit=GMAIL_SWEEP_THREAD_LIMIT
            )
        except google_gmail.GmailError as exc:
            record_failure("sweep_gmail_briefs", exc, target_user_id=creator_id)
            report.failed += 1
            continue
        drafted = 0
        for thread in threads:
            report.scanned += 1
            if drafted >= MAX_GMAIL_DRAFTS_PER_CREATOR_PER_SWEEP:
                break
            if not thread.messages:
                continue
            latest = thread.messages[-1]
            if not latest.is_unread:
                continue
            sender_email = _clean_email(latest.from_)
            if not sender_email:
                continue
            if not _is_brandish_sender(sender_email):
                continue
            dedupe_key = (
                f"gmail_thread:{thread.thread_id}:{latest.message_id}"
            )
            if already_ran("sweep_gmail_briefs", dedupe_key):
                report.skipped_already_ran += 1
                continue
            brand = _brand_from_email(sender_email)
            try:
                babyg_relations.thread_touchpoint(
                    creator_id,
                    kind="email_message",
                    direction="inbound",
                    summary=(latest.subject or "")[:200] or None,
                    brand_name=brand,
                    email=sender_email,
                )
            except Exception:
                logger.info(
                    "sweep_gmail_briefs.thread_touchpoint_failed",
                    exc_info=True,
                )
            try:
                body = _draft_gmail_reply(
                    creator_id=creator_id, thread=thread, brand=brand
                )
            except Exception as exc:
                record_failure(
                    "sweep_gmail_briefs",
                    exc,
                    dedupe_key=dedupe_key,
                    target_user_id=creator_id,
                )
                report.failed += 1
                continue
            if not body:
                mark_ran(
                    "sweep_gmail_briefs",
                    dedupe_key,
                    outcome="skipped",
                    target_user_id=creator_id,
                    detail={"reason": "no_draft"},
                )
                continue
            reply_subject = _reply_subject(latest.subject)
            payload = {
                "to": sender_email,
                "subject": reply_subject,
                "body": body,
                "thread_id": thread.thread_id,
            }
            try:
                proposal = action_proposals.create_proposal(
                    user_id=creator_id,
                    action_type="gmail.create_draft",
                    payload=payload,
                    preview={
                        "title": f"draft reply to {brand}",
                        "to": sender_email,
                        "subject": reply_subject,
                        "body": body,
                        "thread_id": thread.thread_id,
                    },
                    idempotency_key=dedupe_key,
                )
            except Exception as exc:
                record_failure(
                    "sweep_gmail_briefs",
                    exc,
                    dedupe_key=dedupe_key,
                    target_user_id=creator_id,
                )
                report.failed += 1
                continue
            if not proposal:
                continue
            _drop_gmail_nudge(
                creator_id=creator_id,
                proposal=proposal,
                sender_email=sender_email,
                brand=brand,
                subject=latest.subject,
            )
            mark_ran(
                "sweep_gmail_briefs",
                dedupe_key,
                target_user_id=creator_id,
                detail={
                    "thread_id": thread.thread_id,
                    "proposal_id": str(proposal.get("id") or ""),
                    "brand": brand,
                },
            )
            drafted += 1
            report.changed += 1
    return report


def _clean_email(raw: str | None) -> str | None:
    """Extract just the email address from a Gmail `From` header value
    (which may look like `"Anna <anna@brand.example>"` or a bare address)."""
    if not raw:
        return None
    text = str(raw).strip()
    if "<" in text and ">" in text:
        start = text.rfind("<") + 1
        end = text.rfind(">")
        text = text[start:end]
    text = text.strip().lower()
    if "@" not in text:
        return None
    return text


def _is_brandish_sender(email: str) -> bool:
    _, _, domain = email.partition("@")
    root = domain.split(".", 1)[0]
    return bool(root) and root not in _PERSONAL_MAIL_DOMAINS


def _brand_from_email(email: str) -> str:
    _, _, domain = email.partition("@")
    root = domain.split(".", 1)[0] or ""
    return root


def _reply_subject(subject: str | None) -> str:
    s = (subject or "").strip()
    if not s:
        return "re:"
    if s.lower().startswith("re:"):
        return s
    return f"re: {s}"


def _draft_gmail_reply(
    *, creator_id: str, thread, brand: str
) -> str | None:
    """Ask Claude for a short reply body in the creator's voice.
    Bounded max_tokens keeps cost predictable. Returns the reply body
    text, or None if drafting failed / produced nothing usable."""
    profile = profiles.get_creator_profile(creator_id) or {}
    first_name = str(profile.get("full_name") or "").split(" ")[0].strip() or "the creator"
    thread_lines: list[str] = []
    for m in thread.messages[-4:]:
        who = "brand" if _is_brandish_sender(_clean_email(m.from_) or "") else first_name
        body = (m.body_text or m.snippet or "").strip()
        if not body:
            continue
        thread_lines.append(f"{who}: {body[:800]}")
    if not thread_lines:
        return None
    system_prompt = (
        "you are babyg, the creator's ai manager. draft a SHORT reply "
        f"from {first_name} to {brand}. rules: lowercase, no em dashes, "
        "no emojis, no exclamation points, no 'as an ai', tight and "
        "human. do NOT invent numbers or commitments. if the brand asked "
        "a specific question and the answer isn't in the thread, ask "
        "back for the missing detail instead of guessing. output ONLY "
        "the reply body, no subject line, no signature, no explanation."
    )
    messages = [
        {
            "role": "user",
            "content": (
                f"here is the thread with {brand}:\n\n"
                + "\n\n".join(thread_lines)
                + "\n\ndraft the next reply."
            ),
        }
    ]
    try:
        response = anthropic_client.complete_chat(
            system_prompt=system_prompt,
            messages=messages,
            max_tokens=400,
        )
    except (anthropic_client.ClaudeNotConfiguredError, anthropic_client.ClaudeCallError):
        logger.info("sweep_gmail_briefs.draft_call_failed", exc_info=True)
        return None
    text = (response.text or "").strip()
    if not text or len(text) < 10:
        return None
    return text


def _drop_gmail_nudge(
    *,
    creator_id: str,
    proposal: dict[str, Any],
    sender_email: str,
    brand: str,
    subject: str | None,
) -> None:
    """Insert a proposed_action assistant message that points at the
    freshly-staged action_proposal so the creator sees it next time
    they open babyg."""
    subject_line = (subject or "").strip() or "your recent gmail thread"
    content = (
        f"**{brand}** replied about *{subject_line}*. "
        "i staged a draft reply. confirm the card to save it to your "
        "Gmail drafts, or ask me to rewrite it."
    )
    tool_calls = {
        "kind": "proposed_action",
        "status": "pending",
        "action_type": "gmail.create_draft",
        "proposal_id": str(proposal.get("id") or ""),
        "payload": proposal.get("payload") or {},
        "preview": proposal.get("preview") or {},
        "result": None,
        "source": "sweep_gmail_briefs",
    }
    try:
        bot.create_message(
            user_id=creator_id,
            role="assistant",
            content=content,
            tool_calls=tool_calls,
        )
    except Exception:
        logger.info("sweep_gmail_briefs.nudge_insert_failed", exc_info=True)


# --- Sweep runner ----------------------------------------------------------


def run_all(*, now: datetime | None = None) -> list[SweepReport]:
    """Run every registered sweep in order. Returns their reports.
    Called by scripts/run_babyg_sweeps.py from Railway cron."""
    sweeps = [sweep_stale_drafts, sweep_ghosted_deals, sweep_gmail_briefs]
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
