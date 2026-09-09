-- babyg :: 0020 :: unified public discovery cards
-- Requires 0015_restore_brand_profiles.sql and 0019_opportunity_cards.sql.
-- This pre-P6 definition remains valid before 0023 adds richer brand trust fields.

begin;

drop view if exists public.discovery_cards;

create view public.discovery_cards
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
  null::text,
  case when bp.industry is null then '{}'::text[] else array[bp.industry] end,
  bp.created_at,
  null::text,
  null::text,
  null::text,
  null::text,
  case when bp.is_verified then 'verified' else 'unverified' end,
  null::text,
  null::text,
  null::integer,
  null::integer,
  null::timestamptz,
  '/creator/discover/brand/' || bp.user_id::text
from public.brand_profiles bp
where bp.onboarding_completed_at is not null

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
  case when bp.is_verified then 'verified' else 'unverified' end,
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
revoke all on public.discovery_cards from anon, authenticated;

commit;;
