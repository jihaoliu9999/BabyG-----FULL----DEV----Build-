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

create policy creator_discovery_actions_self_select
  on public.creator_discovery_actions
  for select using (user_id = auth.uid() or public.is_operator());

create policy creator_discovery_actions_self_insert
  on public.creator_discovery_actions
  for insert with check (user_id = auth.uid() or public.is_operator());
;
