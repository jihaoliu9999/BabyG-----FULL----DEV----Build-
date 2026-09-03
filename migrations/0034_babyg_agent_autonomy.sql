-- 0034_babyg_agent_autonomy.sql
--
-- Autonomy ladder for the babyg background agent.
--
-- The background agent loop we're about to add (app/services/babyg_agent_loop.py)
-- can take actions on the creator's behalf. This migration adds the
-- per-creator switches that gate what it's allowed to do without a
-- user tap on each individual action.
--
-- Nudges and drafts (staged action_proposals with status='pending')
-- are ALWAYS allowed — that's the minimum for babyg to be useful.
-- Everything below is on top of that baseline.
--
--   babyg_agent_internal_actions   true (default)  can flip its own state
--                                                  (deal stages, memory rewrites,
--                                                  bot_job_runs bookkeeping)
--                                                  without asking. matches
--                                                  today's sweep behavior.
--
--   babyg_agent_gmail_auto_send    false (opt-in)  for narrow, safe patterns
--                                                  (acknowledging a booking,
--                                                  polite decline of an off-brand
--                                                  pitch), agent may send
--                                                  through gmail without a tap.
--                                                  anything ambiguous still
--                                                  stages an action_proposals row.
--
--   babyg_agent_calendar_holds     false (opt-in)  agent may create HOLD events
--                                                  on the creator's own google
--                                                  calendar (transparent,
--                                                  visibility=private, no invites
--                                                  sent to external parties).
--                                                  never sends a real invite
--                                                  without a tap.
--
-- Every write tool the agent calls checks these via
-- app/services/agent_autonomy.py:agent_can(user_id, action). Reads
-- never check — read-only tools are always allowed.
--
-- Defaults preserve today's sweep behavior (internal_actions=true;
-- external writes still gated). A fresh creator is a "safe" creator.

ALTER TABLE public.creator_profiles
  ADD COLUMN IF NOT EXISTS babyg_agent_internal_actions boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS babyg_agent_gmail_auto_send  boolean NOT NULL DEFAULT false,
  ADD COLUMN IF NOT EXISTS babyg_agent_calendar_holds   boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.creator_profiles.babyg_agent_internal_actions IS
  'Agent may flip its own state (deal stages, memory rewrites, sweep bookkeeping) without a per-action tap. Read by agent_autonomy.agent_can.';

COMMENT ON COLUMN public.creator_profiles.babyg_agent_gmail_auto_send IS
  'For narrow safe patterns (booking ack, polite off-brand decline), agent may auto-send via gmail without a per-action tap. Ambiguous replies still stage an action_proposals row. Read by agent_autonomy.agent_can.';

COMMENT ON COLUMN public.creator_profiles.babyg_agent_calendar_holds IS
  'Agent may create HOLD events on the creator''s own google calendar. Never sends invites to external parties without a per-action tap. Read by agent_autonomy.agent_can.';
