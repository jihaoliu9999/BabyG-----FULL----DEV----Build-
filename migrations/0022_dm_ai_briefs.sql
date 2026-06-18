-- 0022_dm_ai_briefs.sql
--
-- P4: babyg's private AI brief for a DM message. The brief is owned by
-- the RECIPIENT of the message (the logged-in user who received it) and
-- is never visible to the other party. RLS enforces recipient-only read;
-- operators can read for abuse review.
--
-- The brief stores only derived analysis (summary, risk, missing terms,
-- next action, optional suggested reply draft) plus small JSON context
-- blobs. It does NOT duplicate raw message bodies — the message lives in
-- dm_messages; message_id links back to it.
--
-- Nothing here sends anything. suggested_reply is a draft the user may
-- copy into the composer; any future external action must still route
-- through action_proposals.

create table if not exists public.dm_ai_briefs (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.dm_threads(id) on delete cascade,
  message_id uuid references public.dm_messages(id) on delete set null,
  recipient_user_id uuid not null references public.users(id) on delete cascade,
  generated_for_role text not null default 'creator'
    check (generated_for_role in ('creator', 'brand', 'operator')),
  generated_at timestamptz not null default now(),
  risk_level text not null default 'unclear' check (risk_level in (
    'safe',
    'unclear',
    'missing_budget',
    'usage_rights_risk',
    'payment_risk',
    'suspicious_identity',
    'inappropriate',
    'unsafe_meetup',
    'adult_minor_risk',
    'scam_phishing',
    'legal_contract_review'
  )),
  risk_reasons jsonb not null default '[]'::jsonb,
  summary text,
  sender_context jsonb not null default '{}'::jsonb,
  missing_terms jsonb not null default '[]'::jsonb,
  recommended_next_action text check (recommended_next_action in (
    'reply',
    'ask_for_budget',
    'request_terms',
    'request_usage_rights',
    'clarify_timeline',
    'ask_for_business_email',
    'schedule_call',
    'decline_politely',
    'flag_for_review',
    'block_or_report',
    'ask_babyg'
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

-- One brief per (message, recipient): regeneration upserts in the service
-- layer. A partial-unique on message_id keeps the common case clean while
-- still allowing a thread-level brief with a null message_id if needed.
create unique index if not exists uq_dm_ai_briefs_message_recipient
  on public.dm_ai_briefs(message_id, recipient_user_id)
  where message_id is not null;

create index if not exists idx_dm_ai_briefs_thread_recipient
  on public.dm_ai_briefs(thread_id, recipient_user_id, generated_at desc);

alter table public.dm_ai_briefs enable row level security;

-- Recipient-only read. The sender of the message can never see the
-- recipient's private brief. Operators can read for abuse review.
create policy dm_ai_briefs_recipient_select
  on public.dm_ai_briefs
  for select using (recipient_user_id = auth.uid() or public.is_operator());

-- Only the recipient (or the service role / operator) writes their brief.
create policy dm_ai_briefs_recipient_insert
  on public.dm_ai_briefs
  for insert with check (recipient_user_id = auth.uid() or public.is_operator());

create policy dm_ai_briefs_recipient_update
  on public.dm_ai_briefs
  for update using (recipient_user_id = auth.uid() or public.is_operator());
