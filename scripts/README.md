# babyg background sweeps · deploy runbook

`run_babyg_sweeps.py` is the entrypoint every scheduled babyg
"analyze in the background" task uses. Five sweeps run in one
invocation, all idempotent per (job_name, dedupe_key) so more
frequent runs are safe — they just no-op faster.

## the five sweeps

| sweep | recommended cadence | what it does |
|---|---|---|
| `sweep_stale_drafts` | every 6 h | flip proposed/edited drafts sitting 14+ days to `stale` |
| `sweep_ghosted_deals` | every 6 h | flip working-stage deals with no touch in 14+ days to `stale_or_ghosted` |
| `sweep_gmail_briefs` | every 15 min | draft replies to fresh brand mail, stage as `gmail.create_draft` action proposals |
| `sweep_dm_briefs` | every 5 min | brief inbound on-site DMs, nudge on watch/alert |
| `sweep_ig_metrics` | daily | snapshot IG account metrics, nudge on outliers |

The recommended combined cadence: run every 5 min with no
`--filter`. The daily/6-hourly sweeps dedupe per (day) internally,
so hitting them every 5 min is harmless — they check `bot_job_runs`
and exit as `skipped_already_ran`.

## Railway setup (production)

1. In the Railway project, add a new service **Cron**. Point it at
   this repo (same source as the web service).
2. **Start command**:
   ```
   python scripts/run_babyg_sweeps.py
   ```
3. **Schedule** (cron syntax, UTC):
   ```
   */5 * * * *
   ```
4. **Env**: reuse the same env group as the web service — the sweep
   needs SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY plus any of
   ANTHROPIC_API_KEY / GOOGLE_CLIENT_* / INSTAGRAM_CLIENT_* to run
   the corresponding sweep. Missing keys degrade gracefully: e.g.
   Gmail sweep skips drafting without `ANTHROPIC_API_KEY`, IG sweep
   skips without `instagram_meta.is_configured()`.

## Running one sweep only (backfill or debug)

```
python scripts/run_babyg_sweeps.py --filter ig
python scripts/run_babyg_sweeps.py --filter gmail,dm
```

Filter is a comma-separated substring match on `SweepReport.job_name`
(case-insensitive). Useful when investigating a specific sweep in
isolation, or after a schema migration to backfill just one path.

## Exit codes

- `0` — the runner reached the end of its sweep list. Per-item
  failures land in `bot_job_failures`; they don't fail the whole
  slot. This is what Railway's alerting should treat as green.
- `1` — the runner itself crashed (import error, missing service
  credentials, config broken). This should page.

## Observability

Every sweep writes to two tables. Both are service-role-only:

- **`bot_job_runs`** — one row per processed unit of work, keyed on
  `(job_name, dedupe_key)`. Includes `outcome` (`ok` / `skipped` /
  `failed`), `target_user_id`, and free-form `detail`. To replay a
  specific unit: delete its row and re-run the sweep.
- **`bot_job_failures`** — one row per unhandled per-item exception,
  including the `dedupe_key` we were mid-processing. Grep the
  `exception_class` to cluster.

Per-creator audit trail also lives in `bot_messages` under
`tool_calls.source LIKE 'sweep_%'` — that's every proactive nudge
each sweep dropped into a bot thread.

## Manual smoke test

From the deployed environment (or any shell with the env set):

```
python scripts/run_babyg_sweeps.py --filter stale_drafts
```

Should print one JSON line per invoked sweep. Zero-work runs still
print (with `scanned: 0, changed: 0`) so the log stream shows the
cron heartbeat.
