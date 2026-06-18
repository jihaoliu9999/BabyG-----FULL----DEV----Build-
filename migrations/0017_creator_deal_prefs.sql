-- 0017_creator_deal_prefs.sql
--
-- Phase 3 of the network/AI rebuild plan: deal preferences section on
-- /creator/profile. Adds three owner-private fields that brand-outreach
-- AI and the upcoming discover-quality ranker consume. None of these
-- ever appear in PUBLIC_CREATOR_FIELDS — they're the creator's own
-- working preferences, not a public spec sheet.
--
--   deal_min_rate_text         free-text floor ("$1.5k organic", "DM
--                              for rates", etc.). NEVER rendered to
--                              peers/brands; surfaced only to babyg
--                              when generating drafts.
--
--   deal_usage_rights_default  the default usage-rights posture babyg
--                              should hold the line on:
--                              'organic_only'    — no paid amplification
--                              'paid_organic'    — paid usage on the
--                                                  creator's channels OK
--                              'paid_with_usage' — full usage rights
--                                                  negotiable for paid
--                              'flexible'        — case-by-case
--
--   deal_travel_willingness    'no'         — no travel
--                              'local_only' — local only (same metro)
--                              'regional'   — regional (same country)
--                              'open'       — open to anywhere
--
-- All defaults preserve current behavior — the creator profile is in
-- a "we don't know yet" state until they fill these in, and downstream
-- consumers must treat null as "ask, don't assume".

ALTER TABLE public.creator_profiles
  ADD COLUMN IF NOT EXISTS deal_min_rate_text text,
  ADD COLUMN IF NOT EXISTS deal_usage_rights_default text,
  ADD COLUMN IF NOT EXISTS deal_travel_willingness text;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_deal_usage_rights_default_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_deal_usage_rights_default_check
        CHECK (
          deal_usage_rights_default IS NULL OR
          deal_usage_rights_default IN (
            'organic_only', 'paid_organic', 'paid_with_usage', 'flexible'
          )
        );
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_deal_travel_willingness_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_deal_travel_willingness_check
        CHECK (
          deal_travel_willingness IS NULL OR
          deal_travel_willingness IN ('no', 'local_only', 'regional', 'open')
        );
  END IF;
END $$;

COMMENT ON COLUMN public.creator_profiles.deal_min_rate_text IS
  'Free-text rate floor. Owner-only; never displayed in public_creator() or any cross-user surface. Surfaced to babyg when generating outreach drafts so suggestions respect the creator''s floor.';

COMMENT ON COLUMN public.creator_profiles.deal_usage_rights_default IS
  'Default usage-rights posture babyg should hold during brand negotiations. Closed vocab: organic_only / paid_organic / paid_with_usage / flexible.';

COMMENT ON COLUMN public.creator_profiles.deal_travel_willingness IS
  'How far the creator will travel for collabs/events. Closed vocab: no / local_only / regional / open.';
