-- 0040_manager_notifications.sql
--
-- Persistent manager activity metadata for notifications.
--
-- This extends the existing notifications table instead of adding a
-- second notification system. Instagram DM content stays in the
-- production Instagram DM tables introduced by 0038:
--   public.instagram_dm_threads
--   public.instagram_dm_messages
--
-- Deliberately does not alter public.dm_threads / public.dm_messages or
-- their RLS policies. Native BabyG DMs keep their existing shape.

alter table public.notifications
  add column if not exists priority text not null default 'normal',
  add column if not exists source_provider text,
  add column if not exists source_event_id text,
  add column if not exists source_thread_id uuid references public.instagram_dm_threads(id) on delete set null,
  add column if not exists underlying_type text,
  add column if not exists underlying_id text,
  add column if not exists metadata jsonb not null default '{}'::jsonb,
  add column if not exists archived_at timestamptz;

alter table public.notifications
  drop constraint if exists notifications_kind_check;

alter table public.notifications
  add constraint notifications_kind_check
  check (kind in (
    'intel_push',
    'booking_reminder',
    'flag_update',
    'collab_match',
    'connection_request',
    'profile_view_digest',
    'job_match',
    'new_dm',
    'manager_alert',
    'profile_sync',
    'performance_spike',
    'system'
  ));

alter table public.notifications
  drop constraint if exists notifications_priority_check;

alter table public.notifications
  add constraint notifications_priority_check
  check (priority in ('low', 'normal', 'high', 'urgent'));

create unique index if not exists uq_notifications_source_event
  on public.notifications(user_id, source_provider, source_event_id)
  where source_event_id is not null;

create index if not exists idx_notifications_user_active_unread
  on public.notifications(user_id, priority, created_at desc)
  where is_read = false and archived_at is null;

comment on column public.notifications.priority is
  'Manager surfacing priority. Used by BabyG home/activity ranking.';
comment on column public.notifications.source_provider is
  'External or internal source namespace for dedupe/deep links, e.g. instagram.';
comment on column public.notifications.source_event_id is
  'Stable provider event key. Unique with user_id/source_provider for webhook retry idempotence.';
comment on column public.notifications.source_thread_id is
  'Instagram DM thread row for notifications backed by public.instagram_dm_threads.';
comment on column public.notifications.underlying_type is
  'Type of real underlying item, e.g. instagram_dm_message.';
comment on column public.notifications.underlying_id is
  'Identifier of the real underlying item.';
comment on column public.notifications.metadata is
  'Small structured context for manager activity rendering and actions.';
comment on column public.notifications.archived_at is
  'Dismiss/archive timestamp. Rows are retained for audit/history.';
