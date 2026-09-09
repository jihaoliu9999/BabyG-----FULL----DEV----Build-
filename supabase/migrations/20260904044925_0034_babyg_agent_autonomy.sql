-- 0034_babyg_agent_autonomy.sql
-- Autonomy ladder for the babyg background agent.
-- Adds per-creator switches gating what the background loop may do
-- without a per-action tap. Defaults preserve today's sweep behavior:
--   internal_actions=true, gmail_auto_send=false, calendar_holds=false.

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
;
