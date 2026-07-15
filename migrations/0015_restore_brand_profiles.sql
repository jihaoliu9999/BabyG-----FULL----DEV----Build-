-- babyg :: 0015 :: restore brand profile foundation
-- Restores the brand profile table dropped in 0007 so the brand role can
-- sign up, onboard, and land on a brand dashboard shell. This does not
-- restore brand outreach, matching, or DM product flows.

create table if not exists public.brand_profiles (
  user_id uuid primary key references public.users(id) on delete cascade,
  company_name text not null default '',
  brand_website text,
  logo_url text,
  industry text,
  contact_full_name text not null default '',
  contact_title text,
  product_description text,
  scale_descriptor text,
  model_descriptor text,
  positioning_descriptor text,
  campaign_types text[] not null default '{}',
  creator_size_preferences text[] not null default '{}',
  niche_preferences text[] not null default '{}',
  budget_range text,
  is_verified boolean not null default false,
  verification_notes text,
  verified_at timestamptz,
  onboarding_completed_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_brand_profiles_industry
  on public.brand_profiles(industry);

create index if not exists idx_brand_profiles_niche_prefs
  on public.brand_profiles using gin (niche_preferences);

drop trigger if exists brand_profiles_set_updated_at on public.brand_profiles;
create trigger brand_profiles_set_updated_at before update on public.brand_profiles
  for each row execute function public.set_updated_at();

alter table public.brand_profiles enable row level security;

drop policy if exists brand_profiles_self_select on public.brand_profiles;
create policy brand_profiles_self_select on public.brand_profiles
  for select using (
    user_id = auth.uid()
    or public.is_operator()
    or public.is_creator()
  );

drop policy if exists brand_profiles_self_write on public.brand_profiles;
create policy brand_profiles_self_write on public.brand_profiles
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());
