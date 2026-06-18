-- 0023_brand_trust.sql
--
-- P6 brand trust system. Two jobs:
--
--   1. Make the discovery_cards view (0020) actually valid: it already
--      selects brand_profiles.verification_status / location_city /
--      location_region, but 0015 never defined those columns. Add them.
--   2. Add the brand-trust data model: a richer verification_status enum,
--      domain/email-domain fields for domain-match checks, the verifying
--      operator, and an operator-only brand_trust_checks ledger.
--
-- Careful language is a product rule, not a DB rule, but the enum tops out
-- at high_risk / blocked as *signals* — never a "fraud" label.

-- 1. brand_profiles: trust columns -------------------------------------------
alter table public.brand_profiles
  add column if not exists verification_status text not null default 'unverified'
    check (verification_status in (
      'unverified',
      'likely_legitimate',
      'verified',
      'needs_review',
      'high_risk',
      'blocked'
    )),
  add column if not exists location_city text,
  add column if not exists location_region text,
  add column if not exists contact_email_domain text,
  add column if not exists website_domain text,
  add column if not exists verified_by_operator_id uuid references public.users(id) on delete set null,
  add column if not exists trust_updated_at timestamptz;

-- Backfill the new enum from the legacy boolean so existing verified
-- brands keep their badge and the view's "<> 'blocked'" filter is sound.
update public.brand_profiles
  set verification_status = 'verified'
  where is_verified = true and verification_status = 'unverified';

create index if not exists idx_brand_profiles_verification_status
  on public.brand_profiles(verification_status);

-- 2. brand_trust_checks: operator-only audit of trust signals ----------------
create table if not exists public.brand_trust_checks (
  id uuid primary key default gen_random_uuid(),
  brand_user_id uuid not null references public.users(id) on delete cascade,
  check_type text not null check (check_type in (
    'domain_match',
    'website_reachable',
    'web_presence',
    'operator_review',
    'creator_report',
    'profile_completeness',
    'email_domain',
    'suspicious_language'
  )),
  result_status text not null check (result_status in (
    'pass',
    'warn',
    'fail',
    'inconclusive'
  )),
  confidence_score numeric,
  details jsonb not null default '{}'::jsonb,
  source_url text,
  created_by_user_id uuid references public.users(id) on delete set null,
  created_by_role text not null default 'system' check (created_by_role in (
    'system',
    'operator',
    'creator_report'
  )),
  created_at timestamptz not null default now()
);

create index if not exists idx_brand_trust_checks_brand_recent
  on public.brand_trust_checks(brand_user_id, created_at desc);

-- Operator-only. Creators and brands must never read raw trust checks;
-- they only ever see the safe projected trust summary computed in the app.
alter table public.brand_trust_checks enable row level security;

create policy brand_trust_checks_operator_select
  on public.brand_trust_checks
  for select using (public.is_operator());

create policy brand_trust_checks_operator_write
  on public.brand_trust_checks
  for all using (public.is_operator()) with check (public.is_operator());
