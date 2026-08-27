-- 0030_babyg_memory_relations.sql
--
-- Phase 3 of the babyg AI v2 plan. See docs/babyg-ai-reference.md.
--
-- Relationship notes: what babyg remembers about how a specific brand or
-- person behaves in business terms. e.g. "Vans paid on time in q3",
-- "Studio Ferm ghosted last time", "Olipop's marketing lead is Anna".
--
-- These are separate from deals because a relationship persists across
-- deals. Studio Ferm may have three separate deals with the creator over
-- the years; the "they ghost past 30 days" note lives on the relationship,
-- not on any single deal.

create table if not exists public.babyg_memory_relationship_notes (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,

  -- One of these three identifies the counterparty. Which one depends
  -- on how they most reliably appear in babyg's data.
  brand_name text,
  brand_id uuid,
  peer_id uuid,

  kind text not null,
  body text not null,
  babyg_source text,

  created_at timestamptz not null default now()
);

create index if not exists babyg_memory_relationship_notes_creator_idx
  on public.babyg_memory_relationship_notes (creator_id, created_at desc);
create index if not exists babyg_memory_relationship_notes_brand_idx
  on public.babyg_memory_relationship_notes (creator_id, lower(brand_name))
  where brand_name is not null;
create index if not exists babyg_memory_relationship_notes_peer_idx
  on public.babyg_memory_relationship_notes (creator_id, peer_id)
  where peer_id is not null;

alter table public.babyg_memory_relationship_notes enable row level security;

drop policy if exists babyg_memory_relationship_notes_owner_select
  on public.babyg_memory_relationship_notes;
create policy babyg_memory_relationship_notes_owner_select
  on public.babyg_memory_relationship_notes
  for select to authenticated
  using (creator_id = auth.uid());

comment on column public.babyg_memory_relationship_notes.kind is
  'payment_reliability | ghost_history | contact_person | past_deal_summary | trust_flag | other';
comment on column public.babyg_memory_relationship_notes.babyg_source is
  'Short trace of where this note came from. e.g. "gmail_thread:abc123", "dm_thread:xyz", "creator_told_me". Never contains raw message bodies.';
