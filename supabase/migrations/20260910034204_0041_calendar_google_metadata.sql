-- 0041_calendar_google_metadata.sql
--
-- Store enough Google Calendar metadata to render real calendar views
-- without faking all-day, timezone, recurring-instance, or cancellation
-- state. Ownership remains bookings.user_id.

alter table public.bookings
  add column if not exists is_all_day boolean not null default false,
  add column if not exists google_timezone text,
  add column if not exists google_recurring_event_id text,
  add column if not exists google_original_start_time jsonb,
  add column if not exists google_status text;

drop index if exists public.bookings_user_google_event_uidx;

alter table public.bookings
  drop constraint if exists bookings_user_id_google_event_id_key;

create unique index if not exists bookings_user_google_calendar_event_uidx
  on public.bookings(user_id, google_calendar_id, google_event_id)
  where google_event_id is not null;

create index if not exists idx_bookings_user_calendar_visible
  on public.bookings(user_id, starts_at, ends_at)
  where status <> 'cancelled';
