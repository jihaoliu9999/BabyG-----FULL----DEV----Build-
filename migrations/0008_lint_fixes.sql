-- babyg :: 0008 :: supabase database linter fixes
-- Addresses findings raised by the Supabase performance/security linter:
--   * 0014_extension_in_public : `citext` is installed in the `public` schema
--   * 0028 / 0029 security_definer_function_executable :
--       `public.rls_auto_enable()` is a SECURITY DEFINER function reachable
--       via `/rest/v1/rpc/rls_auto_enable` by the `anon` and `authenticated`
--       roles
-- Idempotent. Safe to re-run.

-- =============================================================================
-- 1. Relocate the `citext` extension out of the `public` schema.
--    Supabase provisions a dedicated `extensions` schema for exactly this.
--    `ALTER EXTENSION ... SET SCHEMA` moves the type together with its
--    operators and functions; existing columns keep their type reference by
--    OID, so no column drops are required. (The note in 0005 claiming a
--    relocation needs column drops was mistaken — that caveat applies to
--    DROP EXTENSION, not SET SCHEMA.)
--
--    Supabase's API roles already carry `extensions` on their search_path,
--    so unqualified `citext` references continue to resolve.
-- =============================================================================

create schema if not exists extensions;
grant usage on schema extensions to public;

do $$
declare
  citext_schema text;
begin
  select n.nspname
    into citext_schema
    from pg_extension e
    join pg_namespace n on n.oid = e.extnamespace
   where e.extname = 'citext';

  if citext_schema is null then
    -- Extension not installed yet (fresh DB before 0001 ran); nothing to do.
    return;
  end if;

  if citext_schema <> 'extensions' then
    execute 'alter extension citext set schema extensions';
  end if;
end
$$;

-- =============================================================================
-- 2. Lock down `public.rls_auto_enable()`.
--    This SECURITY DEFINER function is an administrative helper, not a public
--    API endpoint, yet PostgREST exposes it as `/rest/v1/rpc/rls_auto_enable`
--    to the `anon` and `authenticated` roles. Revoke EXECUTE from those roles
--    so it can no longer be invoked over the API — the same approach 0005
--    took for the RLS role-helper functions.
-- =============================================================================

do $$
begin
  if exists (
    select 1
      from pg_proc p
      join pg_namespace n on n.oid = p.pronamespace
     where n.nspname = 'public'
       and p.proname = 'rls_auto_enable'
       and p.pronargs = 0
  ) then
    execute 'revoke execute on function public.rls_auto_enable() from public, anon, authenticated';
  end if;
end
$$;
