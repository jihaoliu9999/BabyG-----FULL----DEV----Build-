-- 0036_agent_cycles.sql
--
-- Reasoning trace for every fire of the babyg background agent loop.
-- One row per (creator, cycle) so we can answer:
--
--   * "what did babyg think about my creator at 3:14pm?"
--   * "why did the agent skip cycle 4 in a row?"
--   * "which tool did the agent call most this week?"
--   * "which cycle produced this nudge in my thread?"
--
-- The row is the audit trail; nothing depends on it structurally,
-- which means we can archive/purge old cycles without breaking the
-- runtime loop (agent_daily_spend is the source of truth for
-- budget).
--
-- Shape notes:
--   status                text CHECK  the lifecycle bucket:
--                                       'ok'                  ran an LLM step, executed tools
--                                       'skipped_no_delta'    heuristic pre-filter found nothing new
--                                       'skipped_over_cap'    agent_cost.over_daily_cap()==True
--                                       'skipped_autonomy'    no allowed action for the current settings
--                                       'failed'              exception mid-cycle, see error_class
--   skip_reason           text        free-form context for a skipped/failed status
--   delta                 jsonb       the pre-filter summary that woke the agent
--                                     ({"new_gmail": 3, "new_dms": 1, "ig_outlier": null, ...})
--   tools_called          jsonb       [{"name": "draft_gmail_reply", "args": {...}, "outcome": "ok"}]
--   final_response        text        the agent's final assistant message (nullable)
--   system_prompt_hash    text        sha1 of the system prompt used this cycle;
--                                     lets us bucket old cycles by prompt version
--                                     when we tune the meta-prompt.
--   model                 text        the model actually served ('claude-haiku-4-5-20251001' etc.)
--   prompt_tokens/completion_tokens int / cost_usd numeric(10,6):
--                                     matches agent_daily_spend at the per-cycle grain.
--
-- Indexes:
--   (user_id, cycle_started_at desc)   read the last N cycles for a creator
--   (status, cycle_started_at desc)    scan failed cycles across creators
--
-- RLS:
--   service-role only writes. creators COULD see their own rows
--   later if we build a "why did babyg do X?" view; postponing
--   until we have that UI so we don't ship a permission we don't
--   need yet.

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
