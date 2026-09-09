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
  'Returns the names of all migrations recorded in supabase_migrations.schema_migrations. Used by the FastAPI boot guard to detect drift between repo files and registry.';

revoke execute on function public.applied_migration_names() from public;
revoke execute on function public.applied_migration_names() from anon;
grant  execute on function public.applied_migration_names() to authenticated;
grant  execute on function public.applied_migration_names() to service_role;;
