-- 0031_memory_access_audit.sql
--
-- Phase 3 of the babyg AI v2 plan.
-- Every operator read of a babyg memory row writes one row here.

create table if not exists public.memory_access_audit (
  id uuid primary key default gen_random_uuid(),
  operator_id uuid not null references public.users(id) on delete restrict,
  creator_id uuid not null references public.users(id) on delete cascade,
  memory_kind text not null,
  memory_row_ids jsonb not null default '[]'::jsonb,
  reason text not null,
  created_at timestamptz not null default now()
);

create index if not exists memory_access_audit_creator_idx
  on public.memory_access_audit (creator_id, created_at desc);
create index if not exists memory_access_audit_operator_idx
  on public.memory_access_audit (operator_id, created_at desc);

alter table public.memory_access_audit enable row level security;

-- No authenticated policy. Only the service role reads or writes.

comment on table public.memory_access_audit is
  'Every operator read of babyg memory is logged here. Permanent. See docs/babyg-ai-reference.md phase 3.';
comment on column public.memory_access_audit.memory_kind is
  'drafts | decisions | deals | deal_touchpoints | relationship_notes | voice_samples | contract_flags | creator_preferences';
comment on column public.memory_access_audit.reason is
  'Free-text reason the operator gave for accessing memory. Required. Never NULL.';
;
