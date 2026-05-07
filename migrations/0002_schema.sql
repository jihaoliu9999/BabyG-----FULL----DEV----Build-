-- babyg :: 0002 :: initial schema
-- All tables from product manual §10.2 plus profiles, invite_links, audit_log.
-- RLS is enabled in migration 0003. Apply 0001 before this file.

-- =============================================================================
-- IDENTITY
-- =============================================================================

-- public.users mirrors auth.users 1:1 via the shared UUID. The on-signup
-- trigger that inserts the corresponding row lives in this migration too.
create table if not exists public.users (
  id uuid primary key references auth.users(id) on delete cascade,
  email citext unique not null,
  role text not null check (role in ('creator', 'brand', 'operator')),
  is_active boolean not null default true,
  last_seen_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_users_role on public.users(role);
create trigger users_set_updated_at before update on public.users
  for each row execute function public.set_updated_at();

create table if not exists public.creator_profiles (
  user_id uuid primary key references public.users(id) on delete cascade,
  full_name text,
  instagram_handle citext unique,
  profile_photo_url text,
  city text default 'Miami',
  neighborhood text,
  niches text[] not null default '{}',
  content_formats text[] not null default '{}',
  follower_range text,
  engagement_range text,
  creator_tenure text,
  primary_platform text,
  lifestyle_tags text[] not null default '{}',
  brand_preferences text[] not null default '{}',
  hard_limits text[] not null default '{}',
  writing_samples text[] not null default '{}',
  bio text,
  tier text not null default 'basic' check (tier in ('basic', 'pro', 'vip')),
  sub_bot_persona text not null default 'babyg',
  baseline_followers integer,
  baseline_engagement_rate numeric(5, 2),
  notification_settings jsonb not null default jsonb_build_object(
    'daily_intel_push', true,
    'posting_reminders', true,
    'booking_reminders', true,
    'sms_alerts', false,
    'collab_matches', true,
    'weekly_digest', true
  ),
  onboarding_completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_creator_profiles_tier on public.creator_profiles(tier);
create index if not exists idx_creator_profiles_niches on public.creator_profiles using gin (niches);
create trigger creator_profiles_set_updated_at before update on public.creator_profiles
  for each row execute function public.set_updated_at();

create table if not exists public.brand_profiles (
  user_id uuid primary key references public.users(id) on delete cascade,
  company_name text not null,
  brand_website text,
  logo_url text,
  industry text,
  contact_full_name text,
  contact_title text,
  product_description text,
  scale_descriptor text,
  model_descriptor text,
  positioning_descriptor text,
  campaign_types text[] not null default '{}',
  creator_size_preferences text[] not null default '{}',
  niche_preferences text[] not null default '{}',
  budget_range text,
  is_verified boolean not null default false,
  verification_notes text,
  verified_at timestamptz,
  onboarding_completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_brand_profiles_industry on public.brand_profiles(industry);
create index if not exists idx_brand_profiles_niche_prefs on public.brand_profiles using gin (niche_preferences);
create trigger brand_profiles_set_updated_at before update on public.brand_profiles
  for each row execute function public.set_updated_at();

create table if not exists public.invite_links (
  id uuid primary key default gen_random_uuid(),
  token text not null unique,
  intended_role text not null check (intended_role in ('creator', 'brand', 'operator')),
  invited_email citext,
  created_by uuid references public.users(id) on delete set null,
  used_at timestamptz,
  used_by uuid references public.users(id) on delete set null,
  expires_at timestamptz not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_invite_links_token on public.invite_links(token);
create index if not exists idx_invite_links_unused on public.invite_links(token) where used_at is null;

-- =============================================================================
-- HOT DROPS / INTEL
-- =============================================================================

create table if not exists public.intel_posts (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  body text not null,
  category text not null check (category in ('venue', 'trend', 'brand', 'collab', 'alert')),
  source text,
  confidence text not null default 'medium' check (confidence in ('low', 'medium', 'high')),
  valid_from timestamptz not null default now(),
  valid_until timestamptz not null,
  target_niches text[] not null default '{}',
  target_tiers text[] not null default '{basic, pro, vip}',
  city text not null default 'Miami',
  status text not null default 'draft' check (status in ('draft', 'scheduled', 'active', 'expired', 'archived')),
  scheduled_for timestamptz,
  created_by uuid references public.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_intel_posts_status on public.intel_posts(status);
create index if not exists idx_intel_posts_valid_until on public.intel_posts(valid_until);
create index if not exists idx_intel_posts_target_niches on public.intel_posts using gin (target_niches);
create trigger intel_posts_set_updated_at before update on public.intel_posts
  for each row execute function public.set_updated_at();

create table if not exists public.intel_feedback (
  id uuid primary key default gen_random_uuid(),
  intel_post_id uuid not null references public.intel_posts(id) on delete cascade,
  user_id uuid not null references public.users(id) on delete cascade,
  signal text not null check (signal in ('useful', 'not_useful', 'acted_on')),
  created_at timestamptz not null default now(),
  unique (intel_post_id, user_id)
);
create index if not exists idx_intel_feedback_user on public.intel_feedback(user_id);

create table if not exists public.collab_posts (
  id uuid primary key default gen_random_uuid(),
  title text not null,
  body text not null,
  target_niches text[] not null default '{}',
  valid_from timestamptz not null default now(),
  valid_until timestamptz not null,
  status text not null default 'active' check (status in ('draft', 'active', 'archived')),
  created_by uuid references public.users(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_collab_posts_status on public.collab_posts(status);
create trigger collab_posts_set_updated_at before update on public.collab_posts
  for each row execute function public.set_updated_at();

-- =============================================================================
-- BOT / RECEIPTS / ANALYTICS
-- =============================================================================

create table if not exists public.bot_messages (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  role text not null check (role in ('user', 'assistant', 'system')),
  content text not null,
  tool_calls jsonb,
  proactive_kind text,                                  -- daily_nudge, post_reminder, etc.
  intel_post_id uuid references public.intel_posts(id) on delete set null,
  flagged boolean not null default false,
  flag_category text,                                   -- urgent, soft, scope, brand, booking, complaint
  flag_resolved_at timestamptz,
  flag_resolved_by uuid references public.users(id) on delete set null,
  created_at timestamptz not null default now()
);
create index if not exists idx_bot_messages_user_created on public.bot_messages(user_id, created_at desc);
create index if not exists idx_bot_messages_flagged on public.bot_messages(flagged) where flagged = true;

create table if not exists public.content_receipts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  post_url text,
  post_type text check (post_type in ('reel', 'carousel', 'static', 'story', 'long_form')),
  caption_excerpt text,
  likes_count integer,
  comments_count integer,
  inspired_by_intel_id uuid references public.intel_posts(id) on delete set null,
  posted_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists idx_content_receipts_user_posted on public.content_receipts(user_id, posted_at desc);

create table if not exists public.bot_analytics (
  id uuid primary key default gen_random_uuid(),
  date_utc date not null unique,
  active_creators integer not null default 0,
  total_user_messages integer not null default 0,
  total_assistant_messages integer not null default 0,
  total_proactive_messages integer not null default 0,
  total_tool_calls integer not null default 0,
  total_input_tokens bigint not null default 0,
  total_output_tokens bigint not null default 0,
  total_cost_usd numeric(10, 4) not null default 0,
  flags_urgent integer not null default 0,
  flags_soft integer not null default 0,
  created_at timestamptz not null default now()
);

create table if not exists public.email_drafts (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  recipient_email citext not null,
  subject text not null,
  body text not null,
  source_thread_id text,
  status text not null default 'pending' check (status in ('pending', 'approved', 'sent', 'cancelled')),
  approved_at timestamptz,
  sent_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_email_drafts_user_status on public.email_drafts(user_id, status);
create trigger email_drafts_set_updated_at before update on public.email_drafts
  for each row execute function public.set_updated_at();

-- =============================================================================
-- CALENDAR
-- Google Calendar is the source of truth for events. We persist babyg metadata
-- and the foreign event reference only.
-- =============================================================================

create table if not exists public.bookings (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  title text not null,
  type text not null check (type in ('restaurant', 'event', 'collab', 'brand', 'reminder')),
  starts_at timestamptz not null,
  ends_at timestamptz,
  notes text,
  status text not null default 'confirmed' check (status in ('pending', 'confirmed', 'cancelled')),
  google_calendar_id text,
  google_event_id text,
  venue_name text,
  venue_external_ref text,
  brand_user_id uuid references public.users(id) on delete set null,
  intel_post_id uuid references public.intel_posts(id) on delete set null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, google_event_id)
);
create index if not exists idx_bookings_user_starts on public.bookings(user_id, starts_at desc);
create index if not exists idx_bookings_status on public.bookings(status);
create trigger bookings_set_updated_at before update on public.bookings
  for each row execute function public.set_updated_at();

create table if not exists public.content_reminders (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  kind text not null,                                   -- booking_reminder, booking_followup, posting_reminder, weekly_summary, etc.
  payload jsonb not null default '{}',
  fire_at timestamptz not null,
  fired_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists idx_content_reminders_due on public.content_reminders(fire_at) where fired_at is null;

-- =============================================================================
-- NETWORK
-- =============================================================================

create table if not exists public.creator_connections (
  id uuid primary key default gen_random_uuid(),
  requester_id uuid not null references public.users(id) on delete cascade,
  addressee_id uuid not null references public.users(id) on delete cascade,
  status text not null default 'pending' check (status in ('pending', 'accepted', 'declined', 'blocked')),
  requested_at timestamptz not null default now(),
  responded_at timestamptz,
  check (requester_id <> addressee_id),
  unique (requester_id, addressee_id)
);
create index if not exists idx_creator_connections_addressee on public.creator_connections(addressee_id, status);

create table if not exists public.creator_job_listings (
  id uuid primary key default gen_random_uuid(),
  poster_user_id uuid not null references public.users(id) on delete cascade,
  title text not null,
  description text not null,
  listing_type text not null check (listing_type in ('collab', 'ugc_gig', 'hiring', 'brand_deal')),
  compensation_text text,
  target_niches text[] not null default '{}',
  deadline timestamptz,
  is_active boolean not null default true,
  is_taken_down boolean not null default false,
  taken_down_reason text,
  taken_down_by uuid references public.users(id) on delete set null,
  taken_down_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index if not exists idx_creator_job_listings_active on public.creator_job_listings(is_active, is_taken_down);
create index if not exists idx_creator_job_listings_niches on public.creator_job_listings using gin (target_niches);
create trigger creator_job_listings_set_updated_at before update on public.creator_job_listings
  for each row execute function public.set_updated_at();

create table if not exists public.profile_views (
  id uuid primary key default gen_random_uuid(),
  viewer_id uuid not null references public.users(id) on delete cascade,
  viewed_id uuid not null references public.users(id) on delete cascade,
  viewed_at timestamptz not null default now(),
  check (viewer_id <> viewed_id)
);
create index if not exists idx_profile_views_viewed_at on public.profile_views(viewed_id, viewed_at desc);

-- DM threads and messages. participant_a_id < participant_b_id by convention
-- so the unique constraint dedupes regardless of who initiated.
create table if not exists public.dm_threads (
  id uuid primary key default gen_random_uuid(),
  participant_a_id uuid not null references public.users(id) on delete cascade,
  participant_b_id uuid not null references public.users(id) on delete cascade,
  last_message_at timestamptz,
  created_at timestamptz not null default now(),
  check (participant_a_id < participant_b_id),
  unique (participant_a_id, participant_b_id)
);
create index if not exists idx_dm_threads_a on public.dm_threads(participant_a_id, last_message_at desc);
create index if not exists idx_dm_threads_b on public.dm_threads(participant_b_id, last_message_at desc);

create table if not exists public.dm_messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.dm_threads(id) on delete cascade,
  sender_id uuid not null references public.users(id) on delete cascade,
  body text not null,
  read_at timestamptz,
  created_at timestamptz not null default now()
);
create index if not exists idx_dm_messages_thread_created on public.dm_messages(thread_id, created_at desc);

-- =============================================================================
-- SAFETY / MODERATION / OPS
-- =============================================================================

create table if not exists public.abuse_reports (
  id uuid primary key default gen_random_uuid(),
  reporter_id uuid not null references public.users(id) on delete cascade,
  target_type text not null check (target_type in ('profile', 'listing', 'dm_thread', 'message')),
  target_id uuid not null,
  reason text not null,
  status text not null default 'pending' check (status in ('pending', 'dismissed', 'actioned', 'escalated')),
  reviewed_by uuid references public.users(id) on delete set null,
  reviewed_at timestamptz,
  action_notes text,
  created_at timestamptz not null default now()
);
create index if not exists idx_abuse_reports_status on public.abuse_reports(status);

create table if not exists public.operator_notes (
  id uuid primary key default gen_random_uuid(),
  target_user_id uuid not null references public.users(id) on delete cascade,
  author_user_id uuid not null references public.users(id) on delete set null,
  body text not null,
  created_at timestamptz not null default now()
);
create index if not exists idx_operator_notes_target on public.operator_notes(target_user_id, created_at desc);

create table if not exists public.performance_logs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  week_start_date date not null,
  posts_by_type jsonb not null default '{}',
  engagement_rate numeric(5, 2),
  follower_delta integer,
  active_brand_deals_count integer not null default 0,
  active_brand_deals_value numeric(12, 2) not null default 0,
  notes text,
  entered_by_user_id uuid references public.users(id) on delete set null,
  created_at timestamptz not null default now(),
  unique (user_id, week_start_date)
);
create index if not exists idx_performance_logs_user_week on public.performance_logs(user_id, week_start_date desc);

create table if not exists public.audit_log (
  id uuid primary key default gen_random_uuid(),
  actor_user_id uuid references public.users(id) on delete set null,
  action text not null,
  target_type text,
  target_id uuid,
  notes text,
  created_at timestamptz not null default now()
);
create index if not exists idx_audit_log_actor on public.audit_log(actor_user_id, created_at desc);
create index if not exists idx_audit_log_target on public.audit_log(target_type, target_id);

-- =============================================================================
-- NOTIFICATIONS
-- =============================================================================

create table if not exists public.notifications (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  kind text not null check (kind in (
    'intel_push', 'booking_reminder', 'flag_update', 'collab_match',
    'connection_request', 'profile_view_digest', 'job_match', 'new_dm', 'system'
  )),
  title text not null,
  body text,
  link_path text,
  is_read boolean not null default false,
  created_at timestamptz not null default now(),
  read_at timestamptz
);
create index if not exists idx_notifications_user_unread on public.notifications(user_id, created_at desc) where is_read = false;
