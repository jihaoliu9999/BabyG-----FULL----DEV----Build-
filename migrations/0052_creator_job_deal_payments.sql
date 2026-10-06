-- 0052_creator_job_deal_payments.sql
--
-- Step 7A: the poster (payer) funds an active deal through Stripe Checkout
-- (sandbox). Money routing is a Stripe Connect destination charge: the payer
-- is charged base + 10% babyg fee, the recipient creator's connected account
-- receives base - 10%, and babyg's application fee is the two 10% fees.
--
--   * creator_job_deal_payments -- one row per funding attempt. Amounts are
--                                  integer cents computed by the server from
--                                  the deal; the composite foreign key below
--                                  makes the database refuse any row whose
--                                  deal, payer, recipient or base value does
--                                  not match the deal exactly, and the fee
--                                  CHECK pins the locked 10% / 10% math.
--   * one open attempt per deal -- a partial UNIQUE index allows at most one
--                                  'pending' or 'succeeded' row per deal, so
--                                  a deal can never be paid twice.
--   * creator_job_deals.status  -- widened to 'active' | 'funded'. A deal is
--                                  funded ONLY by the trigger below, in the
--                                  same transaction that marks its payment
--                                  'succeeded' (which the server does only on
--                                  a signature-verified Stripe webhook).
--   * guard trigger             -- a payment's terms never change, and a
--                                  succeeded / failed / expired payment is
--                                  final.
--
-- Privacy posture matches 0048-0051: RLS enabled with no policies,
-- anon/authenticated get nothing, the service role is the only reader.
--
-- Safe to re-run: add column if not exists, drop constraint if exists before
-- add, create ... if not exists, create or replace function, drop trigger if
-- exists before create. No table, column or row is removed; existing deals
-- keep status 'active' and get funded_at = null.
begin;

-- 1. Deals: the funded state ------------------------------------------------

alter table public.creator_job_deals
  add column if not exists funded_at timestamptz;

alter table public.creator_job_deals
  drop constraint if exists creator_job_deals_status_check;
alter table public.creator_job_deals
  add constraint creator_job_deals_status_check
  check (status in ('active', 'funded'));

alter table public.creator_job_deals
  drop constraint if exists creator_job_deals_funded_shape;
alter table public.creator_job_deals
  add constraint creator_job_deals_funded_shape
  check ((status = 'funded') = (funded_at is not null));

-- Target of the payments composite foreign key (id alone is already unique,
-- so this can never reject an existing row).
create unique index if not exists creator_job_deals_payment_terms_key
  on public.creator_job_deals (id, poster_user_id, applicant_user_id, amount_cents);

-- 2. Payments ---------------------------------------------------------------

create table if not exists public.creator_job_deal_payments (
  id uuid primary key default gen_random_uuid(),
  deal_id uuid not null,
  payer_user_id uuid not null,
  recipient_user_id uuid not null,
  base_amount_cents bigint not null,
  payer_fee_cents bigint not null,
  recipient_fee_cents bigint not null,
  total_amount_cents bigint not null,
  recipient_amount_cents bigint not null,
  application_fee_cents bigint not null,
  currency text not null default 'usd' check (currency = 'usd'),
  recipient_stripe_account_id text not null
    check (recipient_stripe_account_id ~ '^acct_[A-Za-z0-9]+$'),
  status text not null default 'pending'
    check (status in ('pending', 'succeeded', 'failed', 'expired')),
  stripe_checkout_session_id text,
  stripe_payment_intent_id text,
  last_stripe_event_id text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  succeeded_at timestamptz,
  constraint creator_job_deal_payments_deal_terms
    foreign key (deal_id, payer_user_id, recipient_user_id, base_amount_cents)
    references public.creator_job_deals (id, poster_user_id, applicant_user_id, amount_cents)
    on delete restrict,
  constraint creator_job_deal_payments_session_unique unique (stripe_checkout_session_id),
  -- Locked economics: 10% (half-up, in cents) on each side.
  constraint creator_job_deal_payments_fee_math check (
    base_amount_cents > 0
    and payer_fee_cents = (base_amount_cents * 1000 + 5000) / 10000
    and recipient_fee_cents = payer_fee_cents
    and total_amount_cents = base_amount_cents + payer_fee_cents
    and recipient_amount_cents = base_amount_cents - recipient_fee_cents
    and application_fee_cents = payer_fee_cents + recipient_fee_cents
    and total_amount_cents between 50 and 99999999
  ),
  constraint creator_job_deal_payments_succeeded_shape check (
    (status = 'succeeded') = (succeeded_at is not null)
    and (status <> 'succeeded' or stripe_checkout_session_id is not null)
  )
);

create unique index if not exists creator_job_deal_payments_one_open_per_deal
  on public.creator_job_deal_payments (deal_id)
  where status in ('pending', 'succeeded');
create index if not exists creator_job_deal_payments_deal_idx
  on public.creator_job_deal_payments (deal_id, created_at desc);

alter table public.creator_job_deal_payments enable row level security;
revoke all on public.creator_job_deal_payments from anon, authenticated;
grant select, insert, update on public.creator_job_deal_payments to service_role;
-- The funding trigger runs as the caller (the service role).
grant update (status, funded_at) on public.creator_job_deals to service_role;

-- 3. Guard: terms are immutable, final states are final ---------------------

create or replace function public.creator_job_deal_payments_guard()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  if (new.deal_id, new.payer_user_id, new.recipient_user_id, new.base_amount_cents,
      new.total_amount_cents, new.application_fee_cents, new.recipient_stripe_account_id)
     is distinct from
     (old.deal_id, old.payer_user_id, old.recipient_user_id, old.base_amount_cents,
      old.total_amount_cents, old.application_fee_cents, old.recipient_stripe_account_id) then
    raise exception 'creator_job_deal_payments: terms are immutable' using errcode = '23514';
  end if;
  if old.status <> 'pending' and new.status is distinct from old.status then
    raise exception 'creator_job_deal_payments: status % is final', old.status using errcode = '23514';
  end if;
  if old.stripe_checkout_session_id is not null
     and new.stripe_checkout_session_id is distinct from old.stripe_checkout_session_id then
    raise exception 'creator_job_deal_payments: checkout session is fixed' using errcode = '23514';
  end if;
  new.updated_at := now();
  return new;
end;
$$;

revoke all on function public.creator_job_deal_payments_guard() from public, anon, authenticated;

drop trigger if exists creator_job_deal_payments_guard on public.creator_job_deal_payments;
create trigger creator_job_deal_payments_guard
  before update on public.creator_job_deal_payments
  for each row
  execute function public.creator_job_deal_payments_guard();

-- 4. Funding: payment succeeded -> deal funded, same transaction ------------

create or replace function public.creator_job_deal_payments_fund_deal()
returns trigger
language plpgsql
set search_path = public, pg_temp
as $$
begin
  update public.creator_job_deals
     set status = 'funded', funded_at = new.succeeded_at
   where id = new.deal_id and status = 'active';
  return new;
end;
$$;

revoke all on function public.creator_job_deal_payments_fund_deal() from public, anon, authenticated;

drop trigger if exists creator_job_deal_payments_fund_deal on public.creator_job_deal_payments;
create trigger creator_job_deal_payments_fund_deal
  after update of status on public.creator_job_deal_payments
  for each row
  when (new.status = 'succeeded' and old.status is distinct from 'succeeded')
  execute function public.creator_job_deal_payments_fund_deal();

comment on table public.creator_job_deal_payments is
  'Step 7A: Stripe Checkout funding attempts for creator_job_deals (destination charge). Server-only access; amounts in integer cents computed server-side from the deal.';
comment on column public.creator_job_deal_payments.total_amount_cents is
  'What the payer is charged: base + 10% payer fee.';
comment on column public.creator_job_deal_payments.recipient_amount_cents is
  'What the recipient connected account receives: base - 10% recipient fee.';
comment on column public.creator_job_deal_payments.application_fee_cents is
  'babyg gross platform fee (payer fee + recipient fee), before Stripe costs.';
comment on column public.creator_job_deals.funded_at is
  'Step 7A: set by the creator_job_deal_payments_fund_deal trigger when a webhook-confirmed payment succeeds.';

commit;
