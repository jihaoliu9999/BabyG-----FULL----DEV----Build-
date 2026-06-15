-- babyg :: 0010 :: action proposals
-- Dedicated approval state for local/external actions. External writes must
-- flow through this table before any provider API is called.

create table if not exists public.action_proposals (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references public.users(id) on delete cascade,
  source_message_id uuid references public.bot_messages(id) on delete set null,
  action_type text not null,
  action_category text not null check (action_category in ('local_write', 'external_write')),
  provider text,
  required_scopes text[] not null default '{}',
  payload jsonb not null default '{}',
  preview jsonb not null default '{}',
  status text not null default 'pending' check (
    status in (
      'pending',
      'confirmed',
      'executing',
      'executed',
      'failed',
      'cancelled',
      'expired'
    )
  ),
  idempotency_key text not null unique,
  external_result_id text,
  error_code text,
  error_message text,
  expires_at timestamptz,
  confirmed_at timestamptz,
  executing_at timestamptz,
  executed_at timestamptz,
  failed_at timestamptz,
  cancelled_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint action_proposals_no_money_actions check (
    action_type !~* '(^payment\.|^purchase\.|^financial\.|^funds\.|^money\.|^ads\.buy|payment|purchase|card|credential|charge|transfer|paid_booking)'
  )
);

create index if not exists idx_action_proposals_user_status
  on public.action_proposals(user_id, status, created_at desc);

create index if not exists idx_action_proposals_source_message
  on public.action_proposals(source_message_id);

drop trigger if exists action_proposals_set_updated_at on public.action_proposals;
create trigger action_proposals_set_updated_at before update on public.action_proposals
  for each row execute function public.set_updated_at();

alter table public.action_proposals enable row level security;

create policy action_proposals_self_select on public.action_proposals
  for select using (user_id = auth.uid() or public.is_operator());

create policy action_proposals_self_insert on public.action_proposals
  for insert with check (user_id = auth.uid() or public.is_operator());

create policy action_proposals_self_update on public.action_proposals
  for update using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());
