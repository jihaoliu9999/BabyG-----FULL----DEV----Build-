-- babyg :: 0007 :: remove brand-side from v1 scope
-- Brand side has been deferred to v1.5 (see brand-side-v1.5 branch
-- for the full hardened code). This migration drops brand-only schema
-- so a fresh deploy doesn't carry empty/dangerous tables.
--
-- What goes:
--   * public.brand_profiles      (and its indexes, trigger, RLS policies)
--   * notifications.kind CHECK constraint loses 'collab_match' (the only
--     brand-only notification kind — used by brand→creator outreach)
--
-- What does NOT go:
--   * The users table (operator + creator still use the role enum
--     value 'brand' historically; the application no longer creates
--     such users and self-signup is closed, but pre-existing rows are
--     left intact so an operator can manually inspect them. v1.5 will
--     re-enable brand signup.)
--   * dm_threads / dm_messages — creators DM each other; the schema
--     is generic over participants.
--   * Any shared notification kinds.

-- 1. Drop the brand_profiles table. CASCADE cleans up the implicit FK
--    edges (`users.id` -> brand_profiles.user_id is cascade-on-delete
--    going the OTHER way, so dropping brand_profiles is safe). All
--    RLS policies, indexes, and the updated_at trigger drop with it.
drop table if exists public.brand_profiles cascade;

-- 2. Tighten the notifications kind CHECK to remove 'collab_match'.
--    Postgres doesn't allow modifying CHECK constraints in place; drop
--    + re-add with the slimmer set.
alter table public.notifications
  drop constraint if exists notifications_kind_check;

-- Any existing 'collab_match' rows are leftover from v1 dev work and
-- have no active read surface (the brand routes that created them
-- have been removed). Re-tag them as 'system' so the CHECK passes;
-- operators can review via the audit_log if needed.
update public.notifications
   set kind = 'system'
 where kind = 'collab_match';

alter table public.notifications
  add constraint notifications_kind_check
  check (kind in (
    'intel_push', 'booking_reminder', 'flag_update',
    'connection_request', 'profile_view_digest', 'job_match', 'new_dm',
    'system'
  ));
