-- 0027_bot_turns.sql
--
-- Phase 1 of the babyg AI v2 plan (see docs/babyg-ai-reference.md).
--
-- Per-turn AI observability. Every babyg turn (chat, confirm, cancel) writes
-- one row here so we can debug behavior after the fact and query aggregates:
--   * which prompt version was live
--   * which tools were requested vs actually executed
--   * whether any guardrail fired (rate floor, override, scope, payment block)
--   * token spend for cost tracking
--   * total wall time vs Anthropic wall time
--
-- Never store secrets, OAuth tokens, cookies, raw Gmail bodies, raw contract
-- text, or private credentials. The columns below are all structured
-- metadata about the turn, not payload content.
--
-- RLS: rows readable only by the owning user_id plus the service role.
-- Operator reads must route through /operator/trust/{user_id}/memory
-- which writes a memory_access_audit row before reading.

create table if not exists public.bot_turns (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  role text not null default 'creator' check (role in ('creator', 'brand', 'operator')),
  conversation_id uuid,
  thread_id uuid,

  -- runtime identity
  model text not null,
  prompt_version text not null,

  -- timing
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  total_duration_ms integer,
  anthropic_duration_ms integer,

  -- tool traces (structured metadata only, no payloads)
  tools_requested jsonb not null default '[]'::jsonb,
  tools_executed jsonb not null default '[]'::jsonb,
  tool_errors jsonb not null default '[]'::jsonb,

  -- action + guardrail traces
  action_proposals_staged jsonb not null default '[]'::jsonb,
  guardrails_triggered jsonb not null default '[]'::jsonb,

  -- cost tracking
  input_tokens integer,
  output_tokens integer,

  -- outcome
  response_type text not null default 'text' check (
    response_type in ('text', 'refusal', 'pending_action', 'error')
  ),
  error_message text,

  -- rollout snapshot
  feature_flags_snapshot jsonb not null default '{}'::jsonb
);

create index if not exists bot_turns_user_started_idx
  on public.bot_turns (user_id, started_at desc);

create index if not exists bot_turns_prompt_version_idx
  on public.bot_turns (prompt_version, started_at desc);

create index if not exists bot_turns_response_type_idx
  on public.bot_turns (response_type, started_at desc);

alter table public.bot_turns enable row level security;

drop policy if exists "bot_turns_owner_select" on public.bot_turns;
create policy "bot_turns_owner_select"
  on public.bot_turns
  for select
  using (auth.uid() = user_id);

drop policy if exists "bot_turns_service_role_all" on public.bot_turns;
create policy "bot_turns_service_role_all"
  on public.bot_turns
  for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

comment on table public.bot_turns is
  'Per-turn AI observability. Metadata only. Never contains raw message bodies, OAuth tokens, or secrets. See docs/babyg-ai-reference.md phase 1.';
comment on column public.bot_turns.tools_requested is
  'List of {name, input_hash?, duration_ms} for tools Claude asked to call this turn.';
comment on column public.bot_turns.tools_executed is
  'List of {name, ok, duration_ms} for tools that actually ran (some can be denied by daily caps).';
comment on column public.bot_turns.guardrails_triggered is
  'List of guardrail names fired this turn. e.g. ["rate_floor_refusal", "override_floor_used", "scope_refusal", "payment_keyword_block"].';
comment on column public.bot_turns.feature_flags_snapshot is
  'Copy of profile.babyg_features at turn time so we can attribute behavior to a rollout cohort.';
