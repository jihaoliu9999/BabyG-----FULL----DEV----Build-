-- babyg :: 0003 :: role helper functions
-- Reads the role from public.users for the currently authenticated user.
-- Lives after the schema migration because the function bodies reference
-- public.users (SQL-language functions are body-checked at create time).
-- Stable + security definer so they can be invoked inside RLS policies
-- without recursive RLS lookups against public.users itself.

create or replace function public.current_user_role()
returns text
language sql
stable
security definer
set search_path = public
as $$
  select role from public.users where id = auth.uid()
$$;

create or replace function public.is_operator()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(public.current_user_role() = 'operator', false)
$$;

create or replace function public.is_creator()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(public.current_user_role() = 'creator', false)
$$;

create or replace function public.is_brand()
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select coalesce(public.current_user_role() = 'brand', false)
$$;
