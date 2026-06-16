-- General creator location fields.
-- Browser coordinates are private owner-side context only; public surfaces
-- should render city/region/country labels, never exact lat/lng.

alter table public.creator_profiles
  add column if not exists location_city text,
  add column if not exists location_region text,
  add column if not exists location_country text,
  add column if not exists location_lat double precision,
  add column if not exists location_lng double precision,
  add column if not exists location_source text,
  add column if not exists location_updated_at timestamptz;

alter table public.creator_profiles
  drop constraint if exists creator_profiles_location_source_check,
  add constraint creator_profiles_location_source_check
    check (location_source is null or location_source in ('manual', 'browser')),
  drop constraint if exists creator_profiles_location_lat_check,
  add constraint creator_profiles_location_lat_check
    check (location_lat is null or (location_lat between -90 and 90)),
  drop constraint if exists creator_profiles_location_lng_check,
  add constraint creator_profiles_location_lng_check
    check (location_lng is null or (location_lng between -180 and 180));
