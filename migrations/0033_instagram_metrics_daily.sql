-- 0033_instagram_metrics_daily.sql
--
-- Instagram Phase C: daily snapshot of account-level metrics per creator.
--
-- babyg only sees a follower NUMBER when it hits the Graph API. To answer
-- "how did your reach grow this week?" we need a time series, not a
-- snapshot. This table stores one row per (user_id, day) with the six
-- account fields we currently pull from Meta:
--
--   followers_count / follows_count / media_count   -- current totals
--   reach / impressions / profile_views             -- last-full-day insights
--
-- Growth is computed by lag over N days at read time. Missing rows are OK —
-- if a day was skipped (Meta cap hit, creator disconnected briefly) the
-- lag just skips further back.
--
-- Service-role only. RLS denies authenticated writes and reads — this is
-- populated by the daily bot_jobs sweep and consumed by the same service
-- layer that already owns Instagram data. Direct creator reads happen via
-- the existing performance / profile services.
--
-- Retention: kept indefinitely. Rows are ~50 bytes each; even at one
-- daily row per active creator for years the table stays trivial.

create table if not exists public.instagram_metrics_daily (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users(id) on delete cascade,
  captured_on date not null,
  followers_count integer,
  follows_count integer,
  media_count integer,
  reach integer,
  impressions integer,
  profile_views integer,
  captured_at timestamptz not null default now(),
  unique (user_id, captured_on)
);

-- Fast reverse-chronological read for `growth_over(user_id, days)`:
--   select ... where user_id = $1 order by captured_on desc limit N
create index if not exists instagram_metrics_daily_user_day_idx
  on public.instagram_metrics_daily (user_id, captured_on desc);

alter table public.instagram_metrics_daily enable row level security;

drop policy if exists instagram_metrics_daily_service_role_all
  on public.instagram_metrics_daily;
create policy instagram_metrics_daily_service_role_all
  on public.instagram_metrics_daily
  for all
  using (auth.role() = 'service_role')
  with check (auth.role() = 'service_role');

comment on table public.instagram_metrics_daily is
  'One row per (creator, day) of Instagram account-level metrics. Populated by the daily bot_jobs snapshot. Growth is computed by lag over N days at read time. See docs/babyg-ai-reference.md (Instagram integration).';
comment on column public.instagram_metrics_daily.captured_on is
  'Calendar day in UTC the snapshot represents. UNIQUE per user so a re-run of the daily job upserts, never duplicates.';
comment on column public.instagram_metrics_daily.captured_at is
  'Wall-clock timestamp of the actual Graph call. Different from captured_on when a make-up sweep back-fills a missed day.';
