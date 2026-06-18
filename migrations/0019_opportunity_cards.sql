-- babyg :: 0019 :: opportunity card fields
-- Extends the existing creator_job_listings table so current postings remain
-- valid while the same rows can participate in unified Discover.

begin;

alter table public.creator_job_listings
  add column if not exists poster_role text not null default 'creator',
  add column if not exists compensation_type text not null default 'unspecified',
  add column if not exists budget_min integer,
  add column if not exists budget_max integer,
  add column if not exists expires_at timestamptz,
  add column if not exists discovery_eligible boolean not null default true,
  add column if not exists location_city text,
  add column if not exists location_region text,
  add column if not exists location_country text;

alter table public.creator_job_listings
  drop constraint if exists creator_job_listings_poster_role_check,
  drop constraint if exists creator_job_listings_compensation_type_check,
  drop constraint if exists creator_job_listings_budget_check;

alter table public.creator_job_listings
  add constraint creator_job_listings_poster_role_check
    check (poster_role in ('creator', 'brand')),
  add constraint creator_job_listings_compensation_type_check
    check (compensation_type in (
      'unspecified', 'flat_rate', 'hourly', 'gifted', 'trade', 'revenue_share'
    )),
  add constraint creator_job_listings_budget_check
    check (
      (budget_min is null or budget_min >= 0)
      and (budget_max is null or budget_max >= 0)
      and (budget_min is null or budget_max is null or budget_min <= budget_max)
    );

create index if not exists idx_creator_job_listings_discovery
  on public.creator_job_listings(discovery_eligible, is_active, is_taken_down, created_at desc);

create index if not exists idx_creator_job_listings_expires
  on public.creator_job_listings(expires_at)
  where discovery_eligible = true and is_active = true and is_taken_down = false;

commit;
