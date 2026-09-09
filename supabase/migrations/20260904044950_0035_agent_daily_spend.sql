-- 0035_agent_daily_spend.sql
-- Daily token + $ spend rollup for the babyg background agent, one
-- row per (creator, UTC date). Populated by agent_cost.record_cycle
-- after every claude call. Read by agent_cost.over_daily_cap to gate
-- the next cycle.

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

create policy agent_daily_spend_service_all on public.agent_daily_spend
  for all using (public.is_operator()) with check (public.is_operator());

comment on table public.agent_daily_spend is
  'Per-creator per-day rollup of babyg agent token spend. Populated by app/services/agent_cost.record_cycle after every claude call. Read by agent_cost.over_daily_cap to gate the next cycle.';
;
