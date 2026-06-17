-- babyg :: 0013 :: creator discovery actions
-- Persistence layer for the swipe-style network discovery experience.
-- Records each pass / connect / view / opened_profile action so the
-- stack remembers what the creator has already seen across sessions.
--
-- Privacy:
--   * RLS restricts row access to the actor (user_id).
--   * The peer (target_user_id) is never told they were passed on.
--   * No row exposes discovery history to other creators.
--
-- This table is append-only. Cooldown for "passed" creators is enforced
-- in the service layer (passed_after_cutoff comparison), not by DELETE,
-- so we keep an audit trail and can adjust the window without losing data.

create table if not exists public.creator_discovery_actions (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  target_user_id uuid not null references public.users(id) on delete cascade,
  action_type text not null check (action_type in (
    'viewed',
    'passed',
    'connected',
    'skipped',
    'opened_profile'
  )),
  created_at timestamptz not null default now(),
  check (user_id <> target_user_id)
);

create index if not exists idx_creator_discovery_actor_recent
  on public.creator_discovery_actions(user_id, action_type, created_at desc);

create index if not exists idx_creator_discovery_actor_target
  on public.creator_discovery_actions(user_id, target_user_id);

alter table public.creator_discovery_actions enable row level security;

-- Self-read: a creator can only see their own discovery actions. The
-- target_user_id never sees the row even if they query it; operators
-- can see all rows via is_operator().
create policy creator_discovery_actions_self_select
  on public.creator_discovery_actions
  for select using (user_id = auth.uid() or public.is_operator());

-- Self-insert: a creator records their own actions.
create policy creator_discovery_actions_self_insert
  on public.creator_discovery_actions
  for insert with check (user_id = auth.uid() or public.is_operator());
