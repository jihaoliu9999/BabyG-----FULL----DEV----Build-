-- 0049_creator_job_offers.sql
--
-- Step 6A: the poster of an opportunity sends ONE structured offer for one
-- application. The offer stores only the proposed BASE deal value plus the
-- terms; no fees, no payment, no deal lifecycle.
--
-- Private, server-owned business terms. Same posture as
-- creator_job_applications (0048): RLS enabled with no policies, no anon or
-- authenticated access, service role only. Every identity column is derived
-- server-side from the listing / application rows, never from the browser.
--
-- Additive only. No DROP, no TRUNCATE, no DELETE, and no change to any
-- existing table. Safe to re-run:
--   * `create table if not exists`  -- no-op on re-run
--   * `alter table ... enable rls`   -- no-op on re-run
--   * `revoke all ...`               -- idempotent
--   * `grant select, insert ...`     -- idempotent
create table if not exists public.creator_job_offers (
  id uuid primary key default gen_random_uuid(),
  application_id uuid not null references public.creator_job_applications(id) on delete cascade,
  listing_id uuid not null references public.creator_job_listings(id) on delete cascade,
  poster_user_id uuid not null references public.users(id) on delete cascade,
  applicant_user_id uuid not null references public.creator_profiles(user_id) on delete cascade,
  -- Integer cents; never floating point. Upper bound is an anti-abuse
  -- ceiling ($10,000,000.00), not a pricing rule.
  amount_cents bigint not null check (amount_cents > 0 and amount_cents <= 1000000000),
  currency text not null default 'USD' check (currency = 'USD'),
  deliverables text not null check (
    char_length(deliverables) <= 2000 and char_length(btrim(deliverables)) >= 1
  ),
  due_date date not null,
  note text check (note is null or char_length(note) <= 2000),
  status text not null default 'sent' check (status = 'sent'),
  created_at timestamptz not null default now(),
  constraint creator_job_offers_one_per_application unique (application_id),
  constraint creator_job_offers_not_self check (poster_user_id <> applicant_user_id)
);

alter table public.creator_job_offers enable row level security;
revoke all on public.creator_job_offers from anon, authenticated;
grant select, insert on public.creator_job_offers to service_role;

comment on table public.creator_job_offers is
  'Step 6A: one private offer per application, sent by the listing poster. authenticated-role has no direct SELECT/INSERT; all access flows through server-side owner-scoped reads/writes.';
comment on column public.creator_job_offers.amount_cents is
  'Proposed BASE deal value in integer cents (USD). No fees applied.';
comment on column public.creator_job_offers.status is
  'Lifecycle marker; Step 6A only emits sent. Later steps may widen the CHECK; never DROP.';
comment on constraint creator_job_offers_one_per_application on public.creator_job_offers is
  'Final duplicate guard: at most one offer per application. The service layer short-circuits duplicates for a cleaner UX.';
