-- 0051_creator_job_deals.sql
--
-- Step 6C: an accepted offer becomes exactly ONE deal.
--
--   * creator_job_deals    -- one row per accepted creator_job_offers row,
--                             carrying the locked terms (amount, currency,
--                             deliverables, due date) and both parties.
--   * creator_job_offers_create_deal trigger
--                          -- creates the deal INSIDE the same transaction
--                             as the accept UPDATE (status -> 'accepted'), so
--                             an accepted offer can never exist without its
--                             deal, and every value is copied from the stored
--                             offer row (nothing comes from the browser).
--   * backfill             -- offers already accepted before this migration
--                             get their deal too.
--
-- Exactly one deal per offer is enforced by a UNIQUE constraint; the
-- trigger and the backfill use ON CONFLICT DO NOTHING, so a repeat or a
-- race can never produce a duplicate. Status is 'active' only -- no payment,
-- completion or dispute states.
--
-- Privacy posture matches 0048-0050: RLS enabled with no policies,
-- anon/authenticated get nothing, the service role is the only reader.
--
-- Additive and safe to re-run: create ... if not exists, create or replace
-- function, drop trigger if exists before create (the same idempotency
-- pattern the repo uses for policies), insert ... on conflict do nothing.
-- No existing table, column or row is altered or removed.
begin;

create table if not exists public.creator_job_deals (
  id uuid primary key default gen_random_uuid(),
  offer_id uuid not null references public.creator_job_offers(id) on delete cascade,
  application_id uuid not null references public.creator_job_applications(id) on delete cascade,
  listing_id uuid not null references public.creator_job_listings(id) on delete cascade,
  poster_user_id uuid not null references public.users(id) on delete cascade,
  applicant_user_id uuid not null references public.creator_profiles(user_id) on delete cascade,
  amount_cents bigint not null check (amount_cents > 0 and amount_cents <= 1000000000),
  currency text not null default 'USD' check (currency = 'USD'),
  deliverables text not null check (
    char_length(deliverables) <= 2000 and char_length(btrim(deliverables)) >= 1
  ),
  due_date date not null,
  status text not null default 'active' check (status = 'active'),
  created_at timestamptz not null default now(),
  constraint creator_job_deals_one_per_offer unique (offer_id),
  constraint creator_job_deals_not_self check (poster_user_id <> applicant_user_id)
);

-- Deals tab: every read is "deals where I am the poster" or "... the applicant".
create index if not exists creator_job_deals_poster_idx
  on public.creator_job_deals (poster_user_id, created_at desc);
create index if not exists creator_job_deals_applicant_idx
  on public.creator_job_deals (applicant_user_id, created_at desc);

alter table public.creator_job_deals enable row level security;
revoke all on public.creator_job_deals from anon, authenticated;
grant select, insert on public.creator_job_deals to service_role;

create or replace function public.creator_job_offers_create_deal()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  insert into public.creator_job_deals (
    offer_id, application_id, listing_id, poster_user_id, applicant_user_id,
    amount_cents, currency, deliverables, due_date
  ) values (
    new.id, new.application_id, new.listing_id, new.poster_user_id, new.applicant_user_id,
    new.amount_cents, new.currency, new.deliverables, new.due_date
  )
  on conflict (offer_id) do nothing;
  return new;
end;
$$;

revoke all on function public.creator_job_offers_create_deal() from public, anon, authenticated;

drop trigger if exists creator_job_offers_create_deal on public.creator_job_offers;
create trigger creator_job_offers_create_deal
  after update of status on public.creator_job_offers
  for each row
  when (new.status = 'accepted' and old.status is distinct from 'accepted')
  execute function public.creator_job_offers_create_deal();

-- Offers accepted before this migration existed.
insert into public.creator_job_deals (
  offer_id, application_id, listing_id, poster_user_id, applicant_user_id,
  amount_cents, currency, deliverables, due_date
)
select
  o.id, o.application_id, o.listing_id, o.poster_user_id, o.applicant_user_id,
  o.amount_cents, o.currency, o.deliverables, o.due_date
from public.creator_job_offers o
where o.status = 'accepted'
on conflict (offer_id) do nothing;

comment on table public.creator_job_deals is
  'Step 6C: one deal per accepted offer, created atomically by trigger. Server-only access; both parties read it through owner-scoped server reads.';
comment on column public.creator_job_deals.amount_cents is
  'Locked BASE deal value in integer cents (USD), copied from the accepted offer. No fees applied.';
comment on column public.creator_job_deals.status is
  'Step 6C emits only active. Later steps may widen the CHECK.';

commit;
