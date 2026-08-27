-- 0029_babyg_memory_deals.sql
--
-- Phase 3+5 of the babyg AI v2 plan. See docs/babyg-ai-reference.md.
--
-- Deals plus deal touchpoints. Every brand DM, email, calendar event,
-- and contract PDF that touches the same brand relationship links to
-- one deal row via a touchpoint row. That gives babyg one canonical
-- object for the whole business relationship instead of scattered
-- summaries per surface.
--
-- Money is stored as int cents to keep arithmetic honest.
--
-- Scoped by creator_id (not user_id) for the same reason as 0028:
-- one account may hold both creator and brand roles.

-- ----- babyg_memory_deals -------------------------------------------------

create table if not exists public.babyg_memory_deals (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  brand_name text not null,
  brand_id uuid,
  handles jsonb not null default '[]'::jsonb,
  emails jsonb not null default '[]'::jsonb,
  stage text not null default 'inquiry' check (stage in (
    'inquiry',
    'negotiating',
    'waiting_on_terms',
    'accepted',
    'delivered',
    'payment_pending',
    'paid',
    'stale_or_ghosted',
    'declined',
    'cancelled'
  )),
  agreed_amount_cents bigint,
  paid_amount_cents bigint,
  deliverables jsonb not null default '[]'::jsonb,
  usage_rights jsonb not null default '{}'::jsonb,
  exclusivity_notes text,
  platform text,
  deadline date,
  payment_terms text,
  notes jsonb not null default '{}'::jsonb,
  first_touch_at timestamptz not null default now(),
  last_touch_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists babyg_memory_deals_creator_last_touch_idx
  on public.babyg_memory_deals (creator_id, last_touch_at desc);
create index if not exists babyg_memory_deals_creator_stage_idx
  on public.babyg_memory_deals (creator_id, stage);
create index if not exists babyg_memory_deals_brand_idx
  on public.babyg_memory_deals (creator_id, lower(brand_name));

alter table public.babyg_memory_deals enable row level security;

drop policy if exists babyg_memory_deals_owner_select on public.babyg_memory_deals;
create policy babyg_memory_deals_owner_select
  on public.babyg_memory_deals
  for select to authenticated
  using (creator_id = auth.uid());

comment on column public.babyg_memory_deals.handles is
  'jsonb array of social handles known to belong to this brand relationship. e.g. ["vansbrand", "vans_official"]. Auto-populated by babyg_relations resolver.';
comment on column public.babyg_memory_deals.emails is
  'jsonb array of email addresses known to belong to this brand relationship.';
comment on column public.babyg_memory_deals.stage is
  'inquiry | negotiating | waiting_on_terms | accepted | delivered | payment_pending | paid | stale_or_ghosted | declined | cancelled. Transitions are code-driven, never model-driven.';

-- ----- babyg_memory_deal_touchpoints --------------------------------------

create table if not exists public.babyg_memory_deal_touchpoints (
  id uuid primary key default gen_random_uuid(),
  deal_id uuid not null references public.babyg_memory_deals(id) on delete cascade,
  creator_id uuid not null references public.users(id) on delete cascade,
  kind text not null check (kind in (
    'dm_message', 'email_message', 'calendar_event',
    'contract_pdf', 'action_proposal', 'note'
  )),
  source_id uuid,
  direction text check (direction in ('inbound', 'outbound', 'internal')),
  stated_amount_cents bigint,
  summary text,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);

create index if not exists babyg_memory_deal_touchpoints_deal_idx
  on public.babyg_memory_deal_touchpoints (deal_id, occurred_at desc);
create index if not exists babyg_memory_deal_touchpoints_creator_idx
  on public.babyg_memory_deal_touchpoints (creator_id, occurred_at desc);

alter table public.babyg_memory_deal_touchpoints enable row level security;

drop policy if exists babyg_memory_deal_touchpoints_owner_select
  on public.babyg_memory_deal_touchpoints;
create policy babyg_memory_deal_touchpoints_owner_select
  on public.babyg_memory_deal_touchpoints
  for select to authenticated
  using (creator_id = auth.uid());

comment on column public.babyg_memory_deal_touchpoints.source_id is
  'Points to the underlying row (dm_message.id, gmail thread id, calendar event id, action_proposal.id, contract_pdf id). Kept as generic uuid so the touchpoint table does not need cross-schema FKs.';
