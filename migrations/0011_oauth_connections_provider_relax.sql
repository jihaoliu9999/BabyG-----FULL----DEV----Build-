-- babyg :: 0011 :: oauth_connections provider relaxation
-- Phase 3 step 1: lets the existing oauth_connections table store
-- Instagram/Meta tokens alongside Google. No new tables. No new
-- columns. The unique (user_id, provider) constraint already in
-- 0009 means a creator can have one Google row + one Instagram
-- row at the same time.
--
-- The constraint name `oauth_connections_provider_check` is the
-- Postgres default for the inline column-level `check (...)` that
-- 0009 defined. `drop ... if exists` covers the unlikely edge case
-- where a hand-applied earlier migration named it differently.
--
-- No service-layer code reads 'instagram' yet — that lands in the
-- Step 2 backend slice (instagram_meta.py + oauth_connections
-- Instagram helpers). This migration is intentionally isolated so
-- a future revert is a single statement.

alter table public.oauth_connections
  drop constraint if exists oauth_connections_provider_check;

alter table public.oauth_connections
  add constraint oauth_connections_provider_check
  check (provider in ('google', 'instagram'));
