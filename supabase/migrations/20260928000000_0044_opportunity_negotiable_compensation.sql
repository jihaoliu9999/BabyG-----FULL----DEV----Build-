-- Extend the existing compensation constraint without changing stored rows.
begin;

alter table public.creator_job_listings
  drop constraint creator_job_listings_compensation_type_check;

alter table public.creator_job_listings
  add constraint creator_job_listings_compensation_type_check
    check (compensation_type in (
      'unspecified', 'flat_rate', 'hourly', 'gifted', 'trade', 'revenue_share',
      'negotiable'
    ));

commit;
