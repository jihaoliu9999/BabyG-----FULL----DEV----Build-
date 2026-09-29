-- 0045_creator_deal_type_preferences.sql
--
-- Deal Preferences follow-up. Creators can now tell babyg which
-- listing_types (canonical opportunity kinds — collab / ugc_gig /
-- hiring / brand_deal — see app/services/jobs.py::LISTING_TYPES and
-- app/routes/opportunities.py::KIND_CHOICES) they want to see first
-- on Discover.
--
-- Owner-private preference, never projected through public_creator().
-- Empty array = "no preference set" and Discover falls back to its
-- current ordering exactly. Preference is a soft signal: matching
-- opportunities are prioritized ABOVE non-matching, but non-matching
-- opportunities and every creator/brand card remain fully visible.
--
-- Additive-only. Safe to re-run: uses ADD COLUMN IF NOT EXISTS and
-- guards the CHECK constraint through pg_constraint.

ALTER TABLE public.creator_profiles
  ADD COLUMN IF NOT EXISTS deal_type_preferences text[]
    NOT NULL DEFAULT '{}'::text[];

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1 FROM pg_constraint
    WHERE conname = 'creator_profiles_deal_type_preferences_check'
  ) THEN
    ALTER TABLE public.creator_profiles
      ADD CONSTRAINT creator_profiles_deal_type_preferences_check
        CHECK (
          deal_type_preferences <@ ARRAY[
            'collab',
            'ugc_gig',
            'hiring',
            'brand_deal'
          ]::text[]
        );
  END IF;
END $$;

COMMENT ON COLUMN public.creator_profiles.deal_type_preferences IS
  'Owner-private list of canonical listing_type values (collab / ugc_gig / hiring / brand_deal) the creator wants prioritized on Discover. Empty array = no preference set; Discover uses its default ordering. Never surfaced through public_creator().';
