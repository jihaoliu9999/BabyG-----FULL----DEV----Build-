-- 0042_instagram_dm_evaluations.sql
--
-- Persist manager evaluations for Instagram DMs ingested by
-- 0038_instagram_dms.sql. This intentionally extends the dedicated
-- Instagram DM tables instead of routing external Instagram peers
-- through native babyg dm_threads/dm_messages.

create table if not exists public.instagram_dm_evaluations (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.instagram_dm_threads(id) on delete cascade,
  message_id uuid references public.instagram_dm_messages(id) on delete set null,
  creator_id uuid not null references public.users(id) on delete cascade,
  result jsonb not null,
  model_id text,
  created_at timestamptz not null default now()
);

create index if not exists idx_instagram_dm_evaluations_creator_thread_recent
  on public.instagram_dm_evaluations(creator_id, thread_id, created_at desc);

alter table public.instagram_dm_evaluations enable row level security;

create policy instagram_dm_evaluations_self_select on public.instagram_dm_evaluations
  for select using (creator_id = auth.uid() or public.is_operator());
create policy instagram_dm_evaluations_service_write on public.instagram_dm_evaluations
  for insert with check (public.is_operator());

comment on table public.instagram_dm_evaluations is
  'AI manager evaluations for Instagram DM threads. Evaluations are owner-scoped by creator_id and source the dedicated Instagram DM message text.';
