ALTER TABLE public.creator_profiles
  ADD COLUMN IF NOT EXISTS dm_preference text NOT NULL DEFAULT 'open',
  ADD COLUMN IF NOT EXISTS location_display_level text NOT NULL DEFAULT 'city',
  ADD COLUMN IF NOT EXISTS babyg_tone text NOT NULL DEFAULT 'casual',
  ADD COLUMN IF NOT EXISTS babyg_risk_tolerance text NOT NULL DEFAULT 'balanced',
  ADD COLUMN IF NOT EXISTS babyg_auto_brief_dms boolean NOT NULL DEFAULT true,
  ADD COLUMN IF NOT EXISTS babyg_email_assistance boolean NOT NULL DEFAULT false;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_dm_preference_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_dm_preference_check
        CHECK (dm_preference IN ('open', 'connections_only'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_location_display_level_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_location_display_level_check
        CHECK (location_display_level IN ('city', 'region', 'hidden'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_babyg_tone_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_babyg_tone_check
        CHECK (babyg_tone IN ('casual', 'professional', 'direct'));
  END IF;

  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_babyg_risk_tolerance_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_babyg_risk_tolerance_check
        CHECK (babyg_risk_tolerance IN ('cautious', 'balanced', 'latitude'));
  END IF;
END $$;

COMMENT ON COLUMN public.creator_profiles.dm_preference IS
  'Who can open a DM thread. ''open'' = anyone (preserves current behavior); ''connections_only'' = accepted connections + opportunity-matched parties only. Enforced server-side in app/services/dms.py.';

COMMENT ON COLUMN public.creator_profiles.location_display_level IS
  'How much of the creator''s location to expose via public_creator(). ''city'' = current behavior (city + region/country fallback); ''region'' = region/country only; ''hidden'' = no location label at all. Exact coordinates are never exposed regardless.';

COMMENT ON COLUMN public.creator_profiles.babyg_tone IS
  'Tone preference for babyg-generated drafts and DM briefs. Read by the agent loop in app/services/bot.py and the upcoming DM brief generator.';

COMMENT ON COLUMN public.creator_profiles.babyg_risk_tolerance IS
  'How aggressively babyg should flag risk in brand outreach and deal terms. ''cautious'' surfaces more risk pills; ''latitude'' surfaces fewer.';

COMMENT ON COLUMN public.creator_profiles.babyg_auto_brief_dms IS
  'When true, every incoming DM auto-triggers a babyg brief. When false, briefs are generated only on explicit "ask babyg" click. Default true per the P7 spec.';

COMMENT ON COLUMN public.creator_profiles.babyg_email_assistance IS
  'When true, babyg may use the Gmail integration (compose drafts only — sends still require action_proposals approval) for assisted outreach. Default false: explicit opt-in.';;
