-- 0039_babyg_agent_ig_auto_send.sql
--
-- Autonomy switch for autonomous Instagram DM sends.
--
-- Extends the ladder introduced in 0034. This column gates the new
-- agent_writes.send_instagram_dm_reply tool, which posts a plain-text
-- DM through Meta's Send API on the creator's behalf without a
-- per-action tap.
--
--   babyg_agent_ig_auto_send   false (opt-in)  agent may reply to an
--                                              existing IG DM thread
--                                              without a tap, for
--                                              obviously safe patterns.
--                                              never initiates a new
--                                              thread. content is
--                                              gated by
--                                              agent_safety.is_instagram_dm_safe
--                                              (no money, urls, phone,
--                                              committal language, first-
--                                              touch sends). Meta also
--                                              enforces its 24-hour
--                                              messaging window at the
--                                              Send API layer.
--
-- Default false so the switch is opt-in. Matches the gmail_auto_send +
-- calendar_holds pattern; each external-write channel gets its own
-- consent. Read by app/services/agent_autonomy.py:agent_can.

ALTER TABLE public.creator_profiles
  ADD COLUMN IF NOT EXISTS babyg_agent_ig_auto_send boolean NOT NULL DEFAULT false;

COMMENT ON COLUMN public.creator_profiles.babyg_agent_ig_auto_send IS
  'Agent may auto-reply to existing Instagram DM threads via Meta''s Send API for narrow safe patterns (never initiates new threads). Content gated by agent_safety.is_instagram_dm_safe. Read by agent_autonomy.agent_can.';
