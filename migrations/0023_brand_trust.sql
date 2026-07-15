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

-- The auth email is the only contact email captured today. Cache only its
-- domain; the address itself remains in public.users and is never projected.
update public.brand_profiles bp
set contact_email_domain = lower(split_part(u.email::text, '@', 2))
from public.users u
where u.id = bp.user_id
  and bp.contact_email_domain is null
  and position('@' in u.email::text) > 1;

-- Keep future brand profiles useful to domain checks without copying the
-- address itself. The trigger runs only on insert; operators may later replace
-- the cached domain with a separately verified business domain.
create or replace function public.set_brand_contact_email_domain()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if new.contact_email_domain is null then
    select lower(split_part(u.email::text, '@', 2))
      into new.contact_email_domain
      from public.users u
      where u.id = new.user_id
        and position('@' in u.email::text) > 1;
  end if;
  return new;
end;
$$;

revoke all on function public.set_brand_contact_email_domain()
  from public, anon, authenticated;
grant execute on function public.set_brand_contact_email_domain()
  to service_role;

drop trigger if exists brand_profiles_set_contact_email_domain
  on public.brand_profiles;
create trigger brand_profiles_set_contact_email_domain
  before insert on public.brand_profiles
  for each row execute function public.set_brand_contact_email_domain();

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
  confidence_score numeric check (
    confidence_score is null or confidence_score between 0 and 1
  ),
  details jsonb not null default '{}'::jsonb
    check (jsonb_typeof(details) = 'object'),
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

drop policy if exists brand_trust_checks_operator_all
  on public.brand_trust_checks;
create policy brand_trust_checks_operator_all
  on public.brand_trust_checks
  for all to authenticated
  using (public.is_operator()) with check (public.is_operator());

revoke all on public.brand_trust_checks from anon, authenticated;
grant select, insert, update, delete on public.brand_trust_checks to service_role;

-- 0020 is intentionally valid before these P6 columns exist so clean installs
-- can apply migrations numerically. Refresh it now to expose the richer trust
-- status and optional brand location while preserving the same column contract.
create or replace view public.discovery_cards
with (security_invoker = true)
as
select
  'creator'::text as card_kind,
  cp.user_id as card_id,
  cp.user_id as owner_user_id,
  coalesce(nullif(cp.full_name, ''), nullif(cp.instagram_handle::text, ''), 'creator') as title,
  case
    when cp.instagram_handle is not null then '@' || cp.instagram_handle::text
    else null
  end as subtitle,
  cp.profile_photo_url as image_url,
  case coalesce(cp.location_display_level, 'city')
    when 'hidden' then null
    when 'region' then nullif(concat_ws(', ', cp.location_region, cp.location_country), '')
    else nullif(concat_ws(', ', cp.location_city, cp.location_region), '')
  end as location_label,
  cp.niches as tags,
  cp.created_at,
  cp.bio as description,
  cp.instagram_handle::text as profile_handle,
  cp.follower_range,
  cp.primary_platform,
  null::text as verification_status,
  null::text as compensation_type,
  null::text as compensation_text,
  null::integer as budget_min,
  null::integer as budget_max,
  null::timestamptz as deadline,
  '/creator/network/' || cp.user_id::text as detail_path
from public.creator_profiles cp
where cp.onboarding_completed_at is not null

union all

select
  'brand'::text as card_kind,
  bp.user_id as card_id,
  bp.user_id as owner_user_id,
  bp.company_name as title,
  bp.industry as subtitle,
  bp.logo_url as image_url,
  nullif(concat_ws(', ', bp.location_city, bp.location_region), '') as location_label,
  case when bp.industry is null then '{}'::text[] else array[bp.industry] end as tags,
  bp.created_at,
  null::text as description,
  null::text as profile_handle,
  null::text as follower_range,
  null::text as primary_platform,
  bp.verification_status,
  null::text as compensation_type,
  null::text as compensation_text,
  null::integer as budget_min,
  null::integer as budget_max,
  null::timestamptz as deadline,
  '/creator/discover/brand/' || bp.user_id::text as detail_path
from public.brand_profiles bp
where bp.onboarding_completed_at is not null
  and bp.verification_status <> 'blocked'

union all

select
  'opportunity'::text as card_kind,
  listing.id as card_id,
  listing.poster_user_id as owner_user_id,
  listing.title,
  coalesce(bp.company_name, cp.full_name, 'creator') as subtitle,
  coalesce(bp.logo_url, cp.profile_photo_url) as image_url,
  coalesce(
    nullif(concat_ws(', ', listing.location_city, listing.location_region), ''),
    nullif(concat_ws(', ', bp.location_city, bp.location_region), ''),
    case coalesce(cp.location_display_level, 'city')
      when 'hidden' then null
      when 'region' then nullif(concat_ws(', ', cp.location_region, cp.location_country), '')
      else nullif(concat_ws(', ', cp.location_city, cp.location_region), '')
    end
  ) as location_label,
  listing.target_niches as tags,
  listing.created_at,
  listing.description,
  null::text as profile_handle,
  null::text as follower_range,
  null::text as primary_platform,
  bp.verification_status,
  listing.compensation_type,
  listing.compensation_text,
  listing.budget_min,
  listing.budget_max,
  listing.deadline,
  '/creator/jobs/' || listing.id::text as detail_path
from public.creator_job_listings listing
left join public.creator_profiles cp on cp.user_id = listing.poster_user_id
left join public.brand_profiles bp on bp.user_id = listing.poster_user_id
where listing.is_active = true
  and listing.is_taken_down = false
  and listing.discovery_eligible = true
  and (listing.expires_at is null or listing.expires_at > now());

grant select on public.discovery_cards to service_role;
revoke all on public.discovery_cards from anon, authenticated;
