-- babyg :: 0006 :: phase 1 audit fixes
-- Apply after 0001-0005. Idempotent where possible.

-- =============================================================================
-- 1. Drop dm_messages_recipient_update_read.
--    The same-table subquery WITH CHECK in 0005 was brittle and unreachable
--    in practice (every write goes through service_role). We don't replace
--    it: read_at updates run via service_role and are gated by the route
--    layer (mark_thread_read_for accepts a reader_id and filters on it).
-- =============================================================================

drop policy if exists dm_messages_recipient_update_read on public.dm_messages;

-- =============================================================================
-- 2. brand_profiles.contact_full_name : tighten to NOT NULL.
--    Onboarding already requires it at the API layer; the schema was
--    permissive. Future writers (the agent surface in Phase 2) shouldn't
--    be able to bypass that.
-- =============================================================================

-- Backfill any existing NULLs with an empty string to satisfy NOT NULL.
update public.brand_profiles
   set contact_full_name = coalesce(contact_full_name, '')
 where contact_full_name is null;

alter table public.brand_profiles
  alter column contact_full_name set not null;

-- =============================================================================
-- 3. profile_views : dedupe per (viewer_id, viewed_id, day).
--    `record_view` writes a row on every page load. Without a uniqueness
--    constraint the table balloons and `count_distinct_viewers` over-fetches
--    to dedupe in Python. Add a per-day unique index so a Python upsert can
--    no-op cleanly. We intentionally key on the date column rather than the
--    timestamp so the same viewer's repeat hits during one day collapse.
--
--    `(timestamptz)::date` isn't immutable (it reads session TimeZone), so
--    Postgres refuses to index it. Pin to UTC explicitly — that expression
--    IS immutable. Repeat hits within the same UTC day collapse, which is
--    what we want for the operator-tier counts.
-- =============================================================================

create unique index if not exists uq_profile_views_per_day
  on public.profile_views (viewer_id, viewed_id, ((viewed_at at time zone 'UTC')::date));

-- =============================================================================
-- 4. intel_posts.target_tiers default : quote the array elements.
--    The previous default `'{basic, pro, vip}'` parses as bare identifiers,
--    which works today but is fragile. Quoted form is the canonical
--    Postgres array literal.
-- =============================================================================

alter table public.intel_posts
  alter column target_tiers set default '{"basic","pro","vip"}'::text[];
