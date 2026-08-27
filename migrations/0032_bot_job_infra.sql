-- 0032_bot_job_infra.sql
--
-- Phase 7 of the babyg AI v2 plan. See docs/babyg-ai-reference.md.
--
-- Background sweep infrastructure. Two tables:
--
--   bot_job_runs     one row per (job_name, dedupe_key). Serves as the
--                    idempotence log so a re-run of the same cron slot,
--                    or a retry of a failed run, never double-processes
--                    the same event. Never delete; the whole point is
--                    that a dedupe key is remembered forever.
--
--   bot_job_failures every unhandled exception during a sweep lands
--                    here with the exception class, message tail, and
--                    the dedupe key we were mid-processing. Retention
--                    stays open for now (14 days would be enough in
--                    steady state; we keep everything until we've seen
--                    the failure surface stabilize).
--
-- Both tables are service-role only. RLS denies authenticated writes
-- and reads — sweep infra is not user-facing, and exception messages
-- may leak internal detail.

create table if not exists public.bot_job_runs (
  id uuid primary key default gen_random_uuid(),
  job_name text not null,
  dedupe_key text not null,
  ran_at timestamptz not null default now(),
  target_user_id uuid,
  outcome text not null default 'ok' check (outcome in ('ok', 'skipped', 'failed')),
  detail jsonb not null default '{}'::jsonb,
  unique (job_name, dedupe_key)
);

create index if not exists bot_job_runs_job_ran_at_idx
  on public.bot_job_runs (job_name, ran_at desc);
create index if not exists bot_job_runs_target_user_idx
  on public.bot_job_runs (target_user_id, ran_at desc)
  where target_user_id is not null;

alter table public.bot_job_runs enable row level security;

drop policy if exists bot_job_runs_service_role_all on public.bot_job_runs;
create policy bot_job_runs_service_role_all
  on public.bot_job_runs
  for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

comment on table public.bot_job_runs is
  'Idempotence log for babyg background sweeps. One row per (job_name, dedupe_key). Permanent. See docs/babyg-ai-reference.md phase 7.';
comment on column public.bot_job_runs.dedupe_key is
  'Stable key that identifies the unit of work. e.g. "stale_draft:<draft_id>" or "ghosted_deal:<deal_id>:<yyyymmdd>". Same key twice means skip.';


create table if not exists public.bot_job_failures (
  id uuid primary key default gen_random_uuid(),
  job_name text not null,
  dedupe_key text,
  exception_class text not null,
  exception_message text,
  target_user_id uuid,
  occurred_at timestamptz not null default now()
);

create index if not exists bot_job_failures_job_time_idx
  on public.bot_job_failures (job_name, occurred_at desc);

alter table public.bot_job_failures enable row level security;

drop policy if exists bot_job_failures_service_role_all on public.bot_job_failures;
create policy bot_job_failures_service_role_all
  on public.bot_job_failures
  for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

comment on table public.bot_job_failures is
  'Every unhandled exception during a babyg background sweep. Includes dedupe_key so we know exactly what was mid-flight. See docs/babyg-ai-reference.md phase 7.';
