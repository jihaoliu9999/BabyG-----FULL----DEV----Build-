-- P6 brand trust system
alter table public.brand_profiles
  add column if not exists verification_status text not null default 'unverified'
    check (verification_status in (
      'unverified', 'likely_legitimate', 'verified', 'needs_review', 'high_risk', 'blocked'
    )),
  add column if not exists location_city text,
  add column if not exists location_region text,
  add column if not exists contact_email_domain text,
  add column if not exists website_domain text,
  add column if not exists verified_by_operator_id uuid references public.users(id) on delete set null,
  add column if not exists trust_updated_at timestamptz;

update public.brand_profiles
set verification_status = 'verified'
where is_verified = true and verification_status = 'unverified';

update public.brand_profiles bp
set contact_email_domain = lower(split_part(u.email::text, '@', 2))
from public.users u
where u.id = bp.user_id
  and bp.contact_email_domain is null
  and position('@' in u.email::text) > 1;

create index if not exists idx_brand_profiles_verification_status
  on public.brand_profiles(verification_status);

create table if not exists public.brand_trust_checks (
  id uuid primary key default gen_random_uuid(),
  brand_user_id uuid not null references public.users(id) on delete cascade,
  check_type text not null check (check_type in (
    'domain_match', 'website_reachable', 'web_presence', 'operator_review',
    'creator_report', 'profile_completeness', 'email_domain', 'suspicious_language'
  )),
  result_status text not null check (result_status in ('pass', 'warn', 'fail', 'inconclusive')),
  confidence_score numeric check (
    confidence_score is null or confidence_score between 0 and 1
  ),
  details jsonb not null default '{}'::jsonb check (jsonb_typeof(details) = 'object'),
  source_url text,
  created_by_user_id uuid references public.users(id) on delete set null,
  created_by_role text not null default 'system'
    check (created_by_role in ('system', 'operator', 'creator_report')),
  created_at timestamptz not null default now()
);

create index if not exists idx_brand_trust_checks_brand_recent
  on public.brand_trust_checks(brand_user_id, created_at desc);

alter table public.brand_trust_checks enable row level security;

drop policy if exists brand_trust_checks_operator_select on public.brand_trust_checks;
create policy brand_trust_checks_operator_select
  on public.brand_trust_checks for select using (public.is_operator());

drop policy if exists brand_trust_checks_operator_write on public.brand_trust_checks;
create policy brand_trust_checks_operator_write
  on public.brand_trust_checks for all
  using (public.is_operator()) with check (public.is_operator());

revoke all on public.brand_trust_checks from anon, authenticated;
grant select, insert, update, delete on public.brand_trust_checks to service_role;

create or replace view public.discovery_cards
with (security_invoker = true)
as
select
  'creator'::text as card_kind,
  cp.user_id as card_id,
  cp.user_id as owner_user_id,
  coalesce(nullif(cp.full_name, ''), nullif(cp.instagram_handle::text, ''), 'creator') as title,
  case when cp.instagram_handle is not null then '@' || cp.instagram_handle::text else null end as subtitle,
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
  'brand'::text,
  bp.user_id,
  bp.user_id,
  bp.company_name,
  bp.industry,
  bp.logo_url,
  nullif(concat_ws(', ', bp.location_city, bp.location_region), ''),
  case when bp.industry is null then '{}'::text[] else array[bp.industry] end,
  bp.created_at,
  null::text,
  null::text,
  null::text,
  null::text,
  bp.verification_status,
  null::text,
  null::text,
  null::integer,
  null::integer,
  null::timestamptz,
  '/creator/discover/brand/' || bp.user_id::text
from public.brand_profiles bp
where bp.onboarding_completed_at is not null
  and bp.verification_status <> 'blocked'

union all

select
  'opportunity'::text,
  listing.id,
  listing.poster_user_id,
  listing.title,
  coalesce(bp.company_name, cp.full_name, 'creator'),
  coalesce(bp.logo_url, cp.profile_photo_url),
  coalesce(
    nullif(concat_ws(', ', listing.location_city, listing.location_region), ''),
    nullif(concat_ws(', ', bp.location_city, bp.location_region), ''),
    case coalesce(cp.location_display_level, 'city')
      when 'hidden' then null
      when 'region' then nullif(concat_ws(', ', cp.location_region, cp.location_country), '')
      else nullif(concat_ws(', ', cp.location_city, cp.location_region), '')
    end
  ),
  listing.target_niches,
  listing.created_at,
  listing.description,
  null::text,
  null::text,
  null::text,
  bp.verification_status,
  listing.compensation_type,
  listing.compensation_text,
  listing.budget_min,
  listing.budget_max,
  listing.deadline,
  '/creator/jobs/' || listing.id::text
from public.creator_job_listings listing
left join public.creator_profiles cp on cp.user_id = listing.poster_user_id
left join public.brand_profiles bp on bp.user_id = listing.poster_user_id
where listing.is_active = true
  and listing.is_taken_down = false
  and listing.discovery_eligible = true
  and (listing.expires_at is null or listing.expires_at > now());

grant select on public.discovery_cards to service_role;
revoke all on public.discovery_cards from anon, authenticated;;
