-- 0036_agent_cycles.sql
-- Reasoning trace for every fire of the babyg background agent loop.
-- One row per (creator, cycle). Written by app/services/agent_cycles.record.
-- Not on any read path of the loop itself — archive/purge freely.

create table if not exists public.agent_cycles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  cycle_started_at timestamptz not null default now(),
  cycle_ended_at timestamptz,
  status text not null check (
    status in (
      'ok',
      'skipped_no_delta',
      'skipped_over_cap',
      'skipped_autonomy',
      'failed'
    )
  ),
  skip_reason text,
  delta jsonb not null default '{}'::jsonb,
  tools_called jsonb not null default '[]'::jsonb,
  final_response text,
  system_prompt_hash text,
  model text,
  prompt_tokens integer not null default 0,
  completion_tokens integer not null default 0,
  cost_usd numeric(10, 6) not null default 0,
  error_class text,
  error_message text,
  created_at timestamptz not null default now()
);

create index if not exists idx_agent_cycles_user_started
  on public.agent_cycles(user_id, cycle_started_at desc);

create index if not exists idx_agent_cycles_status_started
  on public.agent_cycles(status, cycle_started_at desc);

alter table public.agent_cycles enable row level security;

create policy agent_cycles_service_all on public.agent_cycles
  for all using (public.is_operator()) with check (public.is_operator());

comment on table public.agent_cycles is
  'Reasoning trace for babyg agent loop. One row per fire per creator. Written by app/services/agent_cycles.record. Not on any read path of the loop itself — archive/purge freely.';
;
