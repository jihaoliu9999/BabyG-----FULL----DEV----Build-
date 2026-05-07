-- babyg :: 0001 :: extensions and shared triggers
-- Idempotent. Safe to re-run.

create extension if not exists "pgcrypto";
create extension if not exists "citext";

-- Shared updated_at trigger function. Used by every table that has an
-- updated_at column.
create or replace function public.set_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;
