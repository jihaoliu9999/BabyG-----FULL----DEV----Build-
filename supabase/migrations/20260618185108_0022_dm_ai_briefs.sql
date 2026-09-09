-- 0022_dm_ai_briefs.sql
create table if not exists public.dm_ai_briefs (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.dm_threads(id) on delete cascade,
  message_id uuid references public.dm_messages(id) on delete set null,
  recipient_user_id uuid not null references public.users(id) on delete cascade,
  generated_for_role text not null default 'creator'
    check (generated_for_role in ('creator', 'brand', 'operator')),
  generated_at timestamptz not null default now(),
  risk_level text not null default 'unclear' check (risk_level in (
    'safe', 'unclear', 'missing_budget', 'usage_rights_risk', 'payment_risk',
    'suspicious_identity', 'inappropriate', 'unsafe_meetup', 'adult_minor_risk',
    'scam_phishing', 'legal_contract_review'
  )),
  risk_reasons jsonb not null default '[]'::jsonb,
  summary text,
  sender_context jsonb not null default '{}'::jsonb,
  missing_terms jsonb not null default '[]'::jsonb,
  recommended_next_action text check (recommended_next_action in (
    'reply', 'ask_for_budget', 'request_terms', 'request_usage_rights',
    'clarify_timeline', 'ask_for_business_email', 'schedule_call',
    'decline_politely', 'flag_for_review', 'block_or_report', 'ask_babyg'
  )),
  suggested_reply text,
  suggested_reply_status text not null default 'draft'
    check (suggested_reply_status in ('draft', 'used', 'dismissed', 'none')),
  trust_notes jsonb not null default '[]'::jsonb,
  model_id text,
  prompt_hash text,
  generated_by text not null default 'auto' check (generated_by in ('auto', 'manual')),
  created_at timestamptz not null default now()
);

create unique index if not exists uq_dm_ai_briefs_message_recipient
  on public.dm_ai_briefs(message_id, recipient_user_id)
  where message_id is not null;

create index if not exists idx_dm_ai_briefs_thread_recipient
  on public.dm_ai_briefs(thread_id, recipient_user_id, generated_at desc);

alter table public.dm_ai_briefs enable row level security;

create policy dm_ai_briefs_recipient_select
  on public.dm_ai_briefs
  for select using (recipient_user_id = auth.uid() or public.is_operator());

create policy dm_ai_briefs_recipient_insert
  on public.dm_ai_briefs
  for insert with check (recipient_user_id = auth.uid() or public.is_operator());

create policy dm_ai_briefs_recipient_update
  on public.dm_ai_briefs
  for update using (recipient_user_id = auth.uid() or public.is_operator());

revoke all on public.dm_ai_briefs from anon;
grant select, insert, update on public.dm_ai_briefs to authenticated, service_role;;
