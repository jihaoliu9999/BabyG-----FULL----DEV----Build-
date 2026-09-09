-- babyg :: 0006 :: phase 1 audit fixes
-- Apply after 0001-0005. Idempotent where possible.

drop policy if exists dm_messages_recipient_update_read on public.dm_messages;

update public.brand_profiles
   set contact_full_name = coalesce(contact_full_name, '')
 where contact_full_name is null;

alter table public.brand_profiles
  alter column contact_full_name set not null;

create unique index if not exists uq_profile_views_per_day
  on public.profile_views (viewer_id, viewed_id, ((viewed_at at time zone 'UTC')::date));

alter table public.intel_posts
  alter column target_tiers set default '{"basic","pro","vip"}'::text[];
;
