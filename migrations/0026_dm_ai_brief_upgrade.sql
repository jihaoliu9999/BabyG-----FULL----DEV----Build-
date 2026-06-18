-- 0026_dm_ai_brief_upgrade.sql
--
-- P4.1: enrich the existing recipient-private DM brief. This migration is
-- additive; 0022 remains the source table and its recipient-only RLS remains
-- in force. Raw messages are still stored only in dm_messages.

alter table public.dm_ai_briefs
  add column if not exists intent_type text,
  add column if not exists confidence_level text not null default 'low',
  add column if not exists sender_ask text,
  add column if not exists why_it_matters text,
  add column if not exists deal_terms jsonb not null default '{}'::jsonb,
  add column if not exists deal_stage text,
  add column if not exists message_annotations jsonb not null default '[]'::jsonb,
  add column if not exists reply_options jsonb not null default '[]'::jsonb;

alter table public.dm_ai_briefs
  drop constraint if exists dm_ai_briefs_intent_type_check,
  add constraint dm_ai_briefs_intent_type_check check (intent_type is null or intent_type in (
    'paid_campaign', 'gifted', 'collab', 'event', 'affiliate',
    'networking', 'vague', 'suspicious', 'inappropriate',
    'negotiation_follow_up', 'scheduling'
  )),
  drop constraint if exists dm_ai_briefs_confidence_level_check,
  add constraint dm_ai_briefs_confidence_level_check check (
    confidence_level in ('high', 'medium', 'low')
  ),
  drop constraint if exists dm_ai_briefs_deal_stage_check,
  add constraint dm_ai_briefs_deal_stage_check check (deal_stage is null or deal_stage in (
    'new_inquiry', 'qualifying', 'negotiating', 'waiting_terms',
    'scheduled', 'accepted', 'declined', 'risky_hold'
  ));

comment on column public.dm_ai_briefs.deal_terms is
  'Structured, derived deal terms only; never raw message content or payment credentials.';
comment on column public.dm_ai_briefs.reply_options is
  'Recipient-private warm, business, and firm/protective draft options. Never auto-sent.';
