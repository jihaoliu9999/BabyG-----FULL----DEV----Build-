-- babyg :: 0009 :: oauth connections
-- Stores creator-owned OAuth tokens for server-side integrations.

create table if not exists public.oauth_connections (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  provider text not null check (provider in ('google')),
  scopes text[] not null default '{}',
  access_token text not null,
  refresh_token text,
  expires_at timestamptz,
  provider_account_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (user_id, provider)
);

create index if not exists idx_oauth_connections_user_provider
  on public.oauth_connections(user_id, provider);

drop trigger if exists oauth_connections_set_updated_at on public.oauth_connections;
create trigger oauth_connections_set_updated_at before update on public.oauth_connections
  for each row execute function public.set_updated_at();

alter table public.oauth_connections enable row level security;

create policy oauth_connections_self on public.oauth_connections
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());
