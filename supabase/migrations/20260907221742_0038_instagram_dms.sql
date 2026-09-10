-- 0038_instagram_dms.sql
--
-- Instagram DM ingestion tables. Populated by the /webhooks/instagram
-- endpoint (slab #2) when Meta pushes a `messages` webhook event.
--
-- Design decisions:
--
-- **Separate from site-native `dm_messages`/`dm_threads`.** The
-- existing DM schema references babyg `users` on both sides via
-- participant_a_id / participant_b_id foreign keys. IG DM peers are
-- NOT babyg users — they're Instagram accounts we don't have a
-- user row for. Adding "external peer" support to the existing
-- schema would break every RLS policy and every join. Cleaner to
-- keep IG DMs in their own tables and merge them into a unified
-- "inbox view" at read time.
--
-- **`(creator_id, ig_thread_id)` is unique**, not `ig_thread_id`
-- alone. Meta's thread ids are unique within an IG business account,
-- not globally, and two babyg creators could theoretically each
-- receive a DM from the same brand — those are two separate rows.
--
-- **`ig_message_id` unique per creator**, not globally. Same reason
-- as above. This is what gives us idempotent webhook handling:
-- Meta retries webhook deliveries on 5xx, and duplicates just
-- collide on this unique key.
--
-- **`direction` is either 'inbound' or 'outbound'.** Outbound rows
-- are what we write when the creator (or the agent) sends a reply.
-- Meta's webhook returns both directions in the same message event
-- (echo=true for outbound), so this normalizes cleanly.
--
-- **`attachments` is jsonb, not a related table.** Meta returns
-- attachments as a list of {type, url, ...}; we store them verbatim
-- and let the read path pick what to render. Keeps ingestion
-- write-once.

create table if not exists public.instagram_dm_threads (
  id uuid primary key default gen_random_uuid(),
  creator_id uuid not null references public.users(id) on delete cascade,
  ig_thread_id text not null,
  ig_peer_user_id text not null,
  peer_username text,
  last_message_at timestamptz,
  unread_count integer not null default 0,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (creator_id, ig_thread_id)
);

create index if not exists idx_instagram_dm_threads_creator_recent
  on public.instagram_dm_threads(creator_id, last_message_at desc);

drop trigger if exists instagram_dm_threads_set_updated_at
  on public.instagram_dm_threads;
create trigger instagram_dm_threads_set_updated_at
  before update on public.instagram_dm_threads
  for each row execute function public.set_updated_at();

alter table public.instagram_dm_threads enable row level security;

create policy instagram_dm_threads_self_select on public.instagram_dm_threads
  for select using (creator_id = auth.uid() or public.is_operator());
create policy instagram_dm_threads_service_write on public.instagram_dm_threads
  for insert with check (public.is_operator());
create policy instagram_dm_threads_service_update on public.instagram_dm_threads
  for update using (public.is_operator()) with check (public.is_operator());


create table if not exists public.instagram_dm_messages (
  id uuid primary key default gen_random_uuid(),
  thread_id uuid not null references public.instagram_dm_threads(id) on delete cascade,
  creator_id uuid not null references public.users(id) on delete cascade,
  ig_message_id text not null,
  direction text not null check (direction in ('inbound', 'outbound')),
  sender_ig_id text,
  body text,
  attachments jsonb not null default '[]'::jsonb,
  received_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  unique (creator_id, ig_message_id)
);

create index if not exists idx_instagram_dm_messages_thread_time
  on public.instagram_dm_messages(thread_id, received_at desc);
create index if not exists idx_instagram_dm_messages_creator_time
  on public.instagram_dm_messages(creator_id, received_at desc);

alter table public.instagram_dm_messages enable row level security;

create policy instagram_dm_messages_self_select on public.instagram_dm_messages
  for select using (creator_id = auth.uid() or public.is_operator());
create policy instagram_dm_messages_service_write on public.instagram_dm_messages
  for insert with check (public.is_operator());

comment on table public.instagram_dm_threads is
  'Instagram DM threads ingested via the /webhooks/instagram receiver. One row per (babyg creator, IG conversation).';
comment on table public.instagram_dm_messages is
  'Individual IG DM messages. Idempotent on (creator_id, ig_message_id) so Meta webhook retries are safe.';
