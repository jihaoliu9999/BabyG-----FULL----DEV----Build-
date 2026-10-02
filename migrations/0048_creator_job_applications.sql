-- 0048_creator_job_applications.sql
--
-- Step 5B: creator applications to existing opportunity listings.
--
-- Private, server-owned applications. No public or authenticated client
-- access; a future owner-review surface (Step 5C) must use explicit
-- server-side ownership checks against this row, not RLS.
--
-- This file exists so `migrations/` tracks the schema that has ALREADY
-- been manually applied to production and verified via a direct
-- pg_constraint inspection. Running it against a prod that already has
-- the table is safe:
--   * `create table if not exists`     — no-op on re-run
--   * `alter table ... enable rls`     — no-op on re-run
--   * `revoke all ...`                 — idempotent
--   * `grant select, insert ...`       — idempotent
--
-- Additive-only. No DROP, no TRUNCATE, no DELETE.
create table if not exists public.creator_job_applications (
  id uuid primary key default gen_random_uuid(),
  listing_id uuid not null references public.creator_job_listings(id) on delete cascade,
  applicant_user_id uuid not null references public.creator_profiles(user_id) on delete cascade,
  message text not null check (
    char_length(message) <= 2000 and char_length(btrim(message)) >= 1
  ),
  status text not null default 'submitted' check (status = 'submitted'),
  created_at timestamptz not null default now(),
  constraint creator_job_applications_once_per_creator
    unique (listing_id, applicant_user_id)
);

alter table public.creator_job_applications enable row level security;
revoke all on public.creator_job_applications from anon, authenticated;
grant select, insert on public.creator_job_applications to service_role;

comment on table public.creator_job_applications is
  'Private creator applications to creator_job_listings. authenticated-role has no direct SELECT/INSERT — all access flows through server-side owner-scoped reads.';
comment on column public.creator_job_applications.message is
  'Free-text application message. Required, bounded 1..2000 chars by CHECK.';
comment on column public.creator_job_applications.status is
  'Lifecycle marker — Step 5B only emits submitted. Future steps may add additional statuses via ALTER CHECK; never DROP.';
comment on constraint creator_job_applications_once_per_creator on public.creator_job_applications is
  'Final concurrency guard: one application per creator per listing. The service layer additionally short-circuits duplicates for a cleaner UX.';
