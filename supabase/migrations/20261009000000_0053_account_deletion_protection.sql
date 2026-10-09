-- 0053_account_deletion_protection.sql
--
-- Account deletion must never silently delete another person's applications,
-- offers or deals. Before this migration every job-chain foreign key from
-- 0048 / 0049 / 0051 was ON DELETE CASCADE, so deleting a listing, an
-- application, or a poster's / applicant's account removed the other party's
-- records with it.
--
--   * ten foreign keys -> NO ACTION. Postgres now refuses (at the end of the
--     deleting statement, under its own row locks) to delete a listing,
--     application, offer, user or creator profile that an application, offer
--     or deal still references. Race-free: a concurrent insert either
--     commits first and is seen by the check, or waits and then fails its
--     own foreign key check.
--
--       creator_job_applications.listing_id     -> creator_job_listings
--       creator_job_offers.application_id       -> creator_job_applications
--       creator_job_offers.listing_id           -> creator_job_listings
--       creator_job_offers.poster_user_id       -> users
--       creator_job_offers.applicant_user_id    -> creator_profiles
--       creator_job_deals.offer_id              -> creator_job_offers
--       creator_job_deals.application_id        -> creator_job_applications
--       creator_job_deals.listing_id            -> creator_job_listings
--       creator_job_deals.poster_user_id        -> users
--       creator_job_deals.applicant_user_id     -> creator_profiles
--
--     Unchanged on purpose: creator_job_listings.poster_user_id and
--     creator_job_applications.applicant_user_id stay CASCADE. They only
--     remove the deleting user's own posting / own application, and only
--     when nothing above still references it. creator_job_deal_payments ->
--     creator_job_deals stays RESTRICT (0052).
--
--   * public.delete_user_account(uuid) -- deletes one account atomically:
--     the public.users row (and everything that cascades from it) and the
--     auth.users identity (identities and sessions cascade) in a single
--     transaction. Returns one row: 'deleted', 'blocked' (a protected record
--     still references the account; nothing was changed) or 'not_found'
--     (neither row exists). Any other failure raises and rolls everything
--     back. A one-row table rather than a scalar because the pinned
--     postgrest-py client only accepts list-shaped RPC results.
--     SECURITY DEFINER, EXECUTE granted to service_role only.
--
-- No rows are changed by applying this migration; existing rows already
-- satisfy the identical constraints. RLS policies and table grants are
-- untouched. Re-runnable: each constraint is dropped and re-added in one
-- ALTER TABLE statement, the function is create-or-replace, and the final
-- check refuses to commit if any of the ten columns is still CASCADE or
-- carries more than one foreign key.

begin;

alter table public.creator_job_applications
  drop constraint if exists creator_job_applications_listing_id_fkey,
  add constraint creator_job_applications_listing_id_fkey
    foreign key (listing_id) references public.creator_job_listings(id)
    on delete no action;

alter table public.creator_job_offers
  drop constraint if exists creator_job_offers_application_id_fkey,
  add constraint creator_job_offers_application_id_fkey
    foreign key (application_id) references public.creator_job_applications(id)
    on delete no action,
  drop constraint if exists creator_job_offers_listing_id_fkey,
  add constraint creator_job_offers_listing_id_fkey
    foreign key (listing_id) references public.creator_job_listings(id)
    on delete no action,
  drop constraint if exists creator_job_offers_poster_user_id_fkey,
  add constraint creator_job_offers_poster_user_id_fkey
    foreign key (poster_user_id) references public.users(id)
    on delete no action,
  drop constraint if exists creator_job_offers_applicant_user_id_fkey,
  add constraint creator_job_offers_applicant_user_id_fkey
    foreign key (applicant_user_id) references public.creator_profiles(user_id)
    on delete no action;

alter table public.creator_job_deals
  drop constraint if exists creator_job_deals_offer_id_fkey,
  add constraint creator_job_deals_offer_id_fkey
    foreign key (offer_id) references public.creator_job_offers(id)
    on delete no action,
  drop constraint if exists creator_job_deals_application_id_fkey,
  add constraint creator_job_deals_application_id_fkey
    foreign key (application_id) references public.creator_job_applications(id)
    on delete no action,
  drop constraint if exists creator_job_deals_listing_id_fkey,
  add constraint creator_job_deals_listing_id_fkey
    foreign key (listing_id) references public.creator_job_listings(id)
    on delete no action,
  drop constraint if exists creator_job_deals_poster_user_id_fkey,
  add constraint creator_job_deals_poster_user_id_fkey
    foreign key (poster_user_id) references public.users(id)
    on delete no action,
  drop constraint if exists creator_job_deals_applicant_user_id_fkey,
  add constraint creator_job_deals_applicant_user_id_fkey
    foreign key (applicant_user_id) references public.creator_profiles(user_id)
    on delete no action;

-- Refuse to commit if a differently-named CASCADE foreign key survived on any
-- of the ten columns (it would silently defeat the NO ACTION check).
do $$
declare
  bad text;
begin
  select string_agg(format('%s.%s (%s fk, cascade=%s)', t.tbl, t.col, coalesce(f.n, 0),
                           coalesce(f.cascades, 0)), ', ')
    into bad
  from (values
    ('creator_job_applications', 'listing_id'),
    ('creator_job_offers', 'application_id'),
    ('creator_job_offers', 'listing_id'),
    ('creator_job_offers', 'poster_user_id'),
    ('creator_job_offers', 'applicant_user_id'),
    ('creator_job_deals', 'offer_id'),
    ('creator_job_deals', 'application_id'),
    ('creator_job_deals', 'listing_id'),
    ('creator_job_deals', 'poster_user_id'),
    ('creator_job_deals', 'applicant_user_id')
  ) as t(tbl, col)
  left join lateral (
    select count(*) as n, count(*) filter (where c.confdeltype <> 'a') as cascades
    from pg_constraint c
    join pg_attribute a on a.attrelid = c.conrelid and a.attnum = c.conkey[1]
    where c.contype = 'f'
      and c.conrelid = format('public.%I', t.tbl)::regclass
      and cardinality(c.conkey) = 1
      and a.attname = t.col
  ) f on true
  where coalesce(f.n, 0) <> 1 or coalesce(f.cascades, 0) <> 0;

  if bad is not null then
    raise exception '0053: unexpected foreign keys: %', bad;
  end if;
end;
$$;

create or replace function public.delete_user_account(p_user_id uuid)
returns table (result text)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_account uuid;
  v_identity uuid;
begin
  if p_user_id is null then
    raise exception 'delete_user_account: user id is required' using errcode = '22004';
  end if;

  -- One subtransaction: any refusal rolls back both deletes and every
  -- cascaded delete before the function returns.
  begin
    delete from public.users where id = p_user_id returning id into v_account;
    delete from auth.users where id = p_user_id returning id into v_identity;
    if v_identity is null then
      if v_account is not null then
        -- public.users references auth.users, so the identity exists but
        -- could not be deleted: fail loudly instead of reporting not_found.
        raise exception 'delete_user_account: auth identity was not deleted'
          using errcode = 'P0001';
      end if;
      raise exception using errcode = 'no_data_found';
    end if;
  exception
    when foreign_key_violation or restrict_violation then
      return query select 'blocked'::text;
      return;
    when no_data_found then
      return query select 'not_found'::text;
      return;
  end;

  return query select 'deleted'::text;
end;
$$;

comment on function public.delete_user_account(uuid) is
  'Deletes one account atomically: public.users (cascading to owned rows) and the auth.users '
  'identity. Returns one row: deleted | blocked | not_found. Service role only.';

revoke all on function public.delete_user_account(uuid) from public, anon, authenticated;
grant execute on function public.delete_user_account(uuid) to service_role;

commit;
