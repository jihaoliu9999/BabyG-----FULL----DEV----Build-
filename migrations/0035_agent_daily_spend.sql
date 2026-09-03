-- 0035_agent_daily_spend.sql
--
-- Daily token + $ spend rollup for the babyg background agent, one
-- row per (creator, UTC date). The agent loop calls
-- agent_cost.record_cycle after every claude call so we always know
-- how much a creator has burned today. When today's cost_usd crosses
-- the per-creator cap ($0.10 default), the next cycle no-ops until
-- midnight UTC.
--
-- Chosen shape:
--   (user_id, day) unique primary key => idempotent upsert per day.
--   integer token columns => no float rounding drift across cycles.
--   numeric(10, 6) for cost => six decimal places is enough for
--     "did this cycle cost $0.00042" reads without float rounding.
--   cycles_run + last_cycle_at => quick spot-check of "is this
--     creator's agent looping too fast" without joining agent_cycles.
--
-- Read patterns:
--   - agent_cost.over_daily_cap(user_id) hits this table once per
--     agent cycle. index on (user_id, day) serves it.
--   - dashboards can scan a day: select ... where day = current_date;
--     the (day, user_id) partial ordering is fine on a table that
--     grows at N_creators rows/day.

create table if not exists public.agent_daily_spend (
  user_id uuid not null references public.users(id) on delete cascade,
  day date not null,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  cost_usd numeric(10, 6) not null default 0,
  cycles_run integer not null default 0,
  last_cycle_at timestamptz,
  updated_at timestamptz not null default now(),
  primary key (user_id, day)
);

create index if not exists idx_agent_daily_spend_day
  on public.agent_daily_spend(day desc);

drop trigger if exists agent_daily_spend_set_updated_at on public.agent_daily_spend;
create trigger agent_daily_spend_set_updated_at before update on public.agent_daily_spend
  for each row execute function public.set_updated_at();

alter table public.agent_daily_spend enable row level security;

-- Only service-role writes. Creators never see raw dollar spend
-- (the settings UI can surface an aggregate later if we choose).
create policy agent_daily_spend_service_all on public.agent_daily_spend
  for all using (public.is_operator()) with check (public.is_operator());

comment on table public.agent_daily_spend is
  'Per-creator per-day rollup of babyg agent token spend. Populated by app/services/agent_cost.record_cycle after every claude call. Read by agent_cost.over_daily_cap to gate the next cycle. Rows accumulate at N_active_creators per day (small).';
