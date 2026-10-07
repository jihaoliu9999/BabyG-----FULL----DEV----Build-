-- Stripe Connect mapping for creator payout onboarding. Only the trusted
-- server may read or write connected account identifiers.
create table if not exists public.creator_payout_accounts (
  creator_user_id uuid primary key references public.creator_profiles(user_id) on delete cascade,
  stripe_account_id text not null unique,
  created_at timestamptz not null default now()
);

alter table public.creator_payout_accounts enable row level security;

comment on table public.creator_payout_accounts is
  'Server-only mapping between a babyg creator and a Stripe Connect account. No banking or identity data is stored.';
