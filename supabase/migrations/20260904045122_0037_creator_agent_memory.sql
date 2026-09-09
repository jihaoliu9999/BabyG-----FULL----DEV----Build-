-- 0037_creator_agent_memory.sql
-- Durable rolling summary of what babyg knows about a creator, one row
-- per creator + an append-only history table. Loaded into every agent
-- prompt; rewritten by the agent when new info is worth committing;
-- editable by the creator via /creator/profile/settings.

create table if not exists public.creator_agent_memory (
  user_id uuid primary key references public.users(id) on delete cascade,
  summary text not null default '',
  version integer not null default 0,
  updated_by text not null default 'agent' check (updated_by in ('agent', 'user')),
  updated_at timestamptz not null default now()
);

create table if not exists public.creator_agent_memory_history (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  version integer not null,
  summary text not null,
  updated_by text not null check (updated_by in ('agent', 'user')),
  change_reason text,
  created_at timestamptz not null default now()
);

create index if not exists idx_creator_agent_memory_history_user_created
  on public.creator_agent_memory_history(user_id, created_at desc);

alter table public.creator_agent_memory enable row level security;
alter table public.creator_agent_memory_history enable row level security;

create policy creator_agent_memory_self_read on public.creator_agent_memory
  for select using (user_id = auth.uid() or public.is_operator());
create policy creator_agent_memory_self_upsert on public.creator_agent_memory
  for insert with check (user_id = auth.uid() or public.is_operator());
create policy creator_agent_memory_self_update on public.creator_agent_memory
  for update using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

create policy creator_agent_memory_history_self_read on public.creator_agent_memory_history
  for select using (user_id = auth.uid() or public.is_operator());
create policy creator_agent_memory_history_service_write on public.creator_agent_memory_history
  for insert with check (public.is_operator());

comment on table public.creator_agent_memory is
  'Durable rolling summary of what babyg knows about a creator. Read into every agent prompt; rewritten by the agent when new info is worth committing; also editable by the creator via /creator/profile/settings.';

comment on table public.creator_agent_memory_history is
  'Append-only history of every past version of creator_agent_memory. Powers the "what changed" transcript in the settings UI.';
;
