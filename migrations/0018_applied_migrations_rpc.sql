-- 0018_applied_migrations_rpc.sql
--
-- Operational helper that lets the FastAPI app surface migration-drift
-- at boot. The app holds the service-role key but talks to Postgres
-- via PostgREST, which only exposes the `public` schema by default —
-- so it can't read `supabase_migrations.schema_migrations` directly.
--
-- This function projects just the `name` column out as a text[], wrapped
-- in SECURITY DEFINER so callers don't need cross-schema privileges.
-- Returning only names (not version timestamps, not anything else)
-- keeps the surface tiny: migration names are already discoverable
-- from the repo's `migrations/` directory, so nothing sensitive leaks.
--
-- EXECUTE is revoked from `anon` and `public` so unauthenticated calls
-- via /rest/v1/rpc/applied_migration_names get rejected. `authenticated`
-- (i.e. any signed-in user) and the service role can still call it.
--
-- The boot guard is informational by default; flipping the
-- STRICT_MIGRATION_CHECK env var makes it crash the boot when local
-- migration files don't appear in the registry.

create or replace function public.applied_migration_names()
returns text[]
language sql
security definer
set search_path = pg_catalog, public
stable
as $$
  select coalesce(array_agg(name order by name), array[]::text[])
  from supabase_migrations.schema_migrations;
$$;

comment on function public.applied_migration_names() is
  'Returns the names of all migrations recorded in supabase_migrations.schema_migrations. '
  'Used by the FastAPI boot guard to detect drift between repo files and registry.';

revoke execute on function public.applied_migration_names() from public;
revoke execute on function public.applied_migration_names() from anon;
grant  execute on function public.applied_migration_names() to authenticated;
grant  execute on function public.applied_migration_names() to service_role;
