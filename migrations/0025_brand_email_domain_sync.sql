-- babyg :: 0025 :: populate trust email domain for future brands
--
-- 0023 backfilled existing profiles. This trigger keeps later brand signups
-- from producing permanently inconclusive domain checks. It stores only the
-- domain and never exposes or duplicates the full account email.

create or replace function public.set_brand_contact_email_domain()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if new.contact_email_domain is null then
    select lower(split_part(u.email::text, '@', 2))
      into new.contact_email_domain
      from public.users u
      where u.id = new.user_id
        and position('@' in u.email::text) > 1;
  end if;
  return new;
end;
$$;

revoke all on function public.set_brand_contact_email_domain()
  from public, anon, authenticated;
grant execute on function public.set_brand_contact_email_domain()
  to service_role;

drop trigger if exists brand_profiles_set_contact_email_domain
  on public.brand_profiles;
create trigger brand_profiles_set_contact_email_domain
  before insert on public.brand_profiles
  for each row execute function public.set_brand_contact_email_domain();
