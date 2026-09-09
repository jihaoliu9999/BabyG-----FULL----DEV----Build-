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
  'How far the creator will travel for collabs/events. Closed vocab: no / local_only / regional / open.';;
