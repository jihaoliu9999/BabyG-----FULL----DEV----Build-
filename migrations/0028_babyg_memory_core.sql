-- 0028_babyg_memory_core.sql
--
-- Phase 3 of the babyg AI v2 plan. See docs/babyg-ai-reference.md.
--
-- Core memory tables that hold long-lived babyg observations about the
-- creator, keyed by creator_id (not user_id — one account may hold
-- both creator and brand roles once brand side ships, so we scope by
-- role profile from day one).
--
-- Tables in this migration:
--   babyg_memory_drafts          every draft babyg composed for the creator
--   babyg_memory_decisions       decisions the creator made with babyg's help
--   babyg_memory_voice_samples   creator's writing style, from sent messages + edit diffs
--   babyg_memory_creator_preferences  hard preferences ("never nightlife deals", "no gambling")
--   babyg_memory_contract_flags  flagged clauses extracted from contract PDFs
--
-- Deals + touchpoints live in 0029. Relationship notes live in 0030.
-- The operator access audit table lives in 0031.
--
-- RLS: selects are locked to auth.uid() = creator_id. Operator reads
-- MUST route through /operator/trust/{creator_id}/memory which uses
-- the service role AND writes a memory_access_audit row first. There
-- is intentionally NO or public.is_operator() escape here.
--
-- Retention rule (enforced in code, not in DB): never delete. Only the
-- last 12 months preload into the system prompt; older memory reads
-- via explicit tool calls with a date range.

-- ----- babyg_memory_drafts -------------------------------------------------

create table if not exists public.babyg_memory_drafts (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  channel text not null check (channel in ('dm', 'email')),
  origin_tool text,
  thread_id uuid,
  peer_id uuid,
  deal_id uuid,
  subject text,
  to_addr text,
  body text not null,
  status text not null default 'proposed' check (
    status in ('proposed', 'edited', 'approved', 'sent', 'canceled', 'stale')
  ),
  gmail_message_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  sent_at timestamptz
);

create index if not exists babyg_memory_drafts_creator_updated_idx
  on public.babyg_memory_drafts (creator_id, updated_at desc);
create index if not exists babyg_memory_drafts_creator_status_idx
  on public.babyg_memory_drafts (creator_id, status, updated_at desc);
create index if not exists babyg_memory_drafts_deal_idx
  on public.babyg_memory_drafts (deal_id) where deal_id is not null;
create index if not exists babyg_memory_drafts_thread_idx
  on public.babyg_memory_drafts (thread_id) where thread_id is not null;

alter table public.babyg_memory_drafts enable row level security;

drop policy if exists babyg_memory_drafts_owner_select on public.babyg_memory_drafts;
create policy babyg_memory_drafts_owner_select
  on public.babyg_memory_drafts
  for select to authenticated
  using (creator_id = auth.uid());

comment on table public.babyg_memory_drafts is
  'Every draft babyg composes stays here by default. Gmail drafts saved to Gmail only when creator explicitly asks. See docs/babyg-ai-reference.md phase 4.';
comment on column public.babyg_memory_drafts.status is
  'proposed | edited | approved | sent | canceled | stale (unsent + unedited for 14+ days).';
comment on column public.babyg_memory_drafts.gmail_message_id is
  'Filled only when the draft was actually sent through Gmail. NULL for drafts babyg composed but the creator never sent.';

-- ----- babyg_memory_decisions ---------------------------------------------

create table if not exists public.babyg_memory_decisions (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  kind text not null,
  summary text not null,
  context jsonb not null default '{}'::jsonb,
  deal_id uuid,
  created_at timestamptz not null default now()
);

create index if not exists babyg_memory_decisions_creator_created_idx
  on public.babyg_memory_decisions (creator_id, created_at desc);
create index if not exists babyg_memory_decisions_deal_idx
  on public.babyg_memory_decisions (deal_id) where deal_id is not null;

alter table public.babyg_memory_decisions enable row level security;

drop policy if exists babyg_memory_decisions_owner_select on public.babyg_memory_decisions;
create policy babyg_memory_decisions_owner_select
  on public.babyg_memory_decisions
  for select to authenticated
  using (creator_id = auth.uid());

comment on table public.babyg_memory_decisions is
  'Structured record of decisions the creator made with babyg. e.g. "passed on Nike gifting", "counter Vans at $2k". See docs/babyg-ai-reference.md phase 3.';

-- ----- babyg_memory_voice_samples -----------------------------------------

create table if not exists public.babyg_memory_voice_samples (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  source text not null check (source in ('sent_message', 'edit_diff', 'chip_tap')),
  channel text check (channel in ('dm', 'email', 'chat', 'chip')),
  body text not null,
  babyg_draft_body text,
  created_at timestamptz not null default now()
);

create index if not exists babyg_memory_voice_samples_creator_created_idx
  on public.babyg_memory_voice_samples (creator_id, created_at desc);
create index if not exists babyg_memory_voice_samples_source_idx
  on public.babyg_memory_voice_samples (creator_id, source, created_at desc);

alter table public.babyg_memory_voice_samples enable row level security;

drop policy if exists babyg_memory_voice_samples_owner_select on public.babyg_memory_voice_samples;
create policy babyg_memory_voice_samples_owner_select
  on public.babyg_memory_voice_samples
  for select to authenticated
  using (creator_id = auth.uid());

comment on column public.babyg_memory_voice_samples.source is
  'sent_message: raw creator send. edit_diff: creator edited babyg draft before sending (babyg_draft_body is the original). chip_tap: creator tapped a pre-canned chip.';

-- ----- babyg_memory_creator_preferences -----------------------------------

create table if not exists public.babyg_memory_creator_preferences (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  key text not null,
  value jsonb not null default 'true'::jsonb,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (creator_id, key)
);

create index if not exists babyg_memory_creator_preferences_creator_idx
  on public.babyg_memory_creator_preferences (creator_id);

alter table public.babyg_memory_creator_preferences enable row level security;

drop policy if exists babyg_memory_creator_preferences_owner_select
  on public.babyg_memory_creator_preferences;
create policy babyg_memory_creator_preferences_owner_select
  on public.babyg_memory_creator_preferences
  for select to authenticated
  using (creator_id = auth.uid());

comment on table public.babyg_memory_creator_preferences is
  'Hard preferences babyg has learned or the creator has stated. e.g. key="no_nightlife_deals" value=true, key="preferred_shoot_days" value=["fri","sat"].';

-- ----- babyg_memory_contract_flags ----------------------------------------

create table if not exists public.babyg_memory_contract_flags (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  contract_ref text,
  deal_id uuid,
  clause_kind text not null,
  severity text not null default 'watch' check (severity in ('watch', 'concern', 'blocker')),
  excerpt text not null,
  babyg_note text,
  created_at timestamptz not null default now()
);

create index if not exists babyg_memory_contract_flags_creator_idx
  on public.babyg_memory_contract_flags (creator_id, created_at desc);
create index if not exists babyg_memory_contract_flags_deal_idx
  on public.babyg_memory_contract_flags (deal_id) where deal_id is not null;

alter table public.babyg_memory_contract_flags enable row level security;

drop policy if exists babyg_memory_contract_flags_owner_select
  on public.babyg_memory_contract_flags;
create policy babyg_memory_contract_flags_owner_select
  on public.babyg_memory_contract_flags
  for select to authenticated
  using (creator_id = auth.uid());

comment on column public.babyg_memory_contract_flags.clause_kind is
  'exclusivity | usage_rights | payment_terms | kill_fee | revisions | approval_rights | ownership | other';
