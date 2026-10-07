-- 0046_discovery_cards_listing_type.sql
--
-- Discover preference-first ordering needs to know each opportunity
-- card's canonical listing_type (collab / ugc_gig / hiring /
-- brand_deal). The discovery_cards view (0020, refreshed by 0023)
-- doesn't currently expose it. This migration re-declares the view
-- with one extra column — listing_type text — populated from
-- creator_job_listings.listing_type on opportunity rows and NULL on
-- creator/brand rows.
--
-- Every other column is identical to the 0023 definition so app code
-- reading the view keeps working unchanged. Safe to re-run:
-- create or replace view.
--
-- Grants match 0023 exactly.

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
  null::text as listing_type,
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
  null::text as listing_type,
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
  listing.listing_type,
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
