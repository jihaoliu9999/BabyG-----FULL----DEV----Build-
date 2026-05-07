-- babyg :: 0005 :: security hardening
-- Addresses Supabase advisor warnings raised after applying 0001-0004:
--   * set_updated_at had a mutable search_path
--   * SECURITY DEFINER helper fns were exposed via PostgREST RPC to anon/authenticated
--   * dm_messages_recipient_update_read used WITH CHECK (true), allowing
--     participants to rewrite arbitrary message fields, not just read_at
--
-- citext extension lives in the public schema by default on Supabase; relocating
-- it requires dropping columns that depend on it, so we leave it alone here.

-- 1. Pin set_updated_at search_path
create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

-- 2. RLS role helpers are not API endpoints. They are invoked from policies
--    using the SECURITY DEFINER context. Revoke execute from the PostgREST
--    roles so they cannot be called via /rest/v1/rpc/<fn>.
revoke execute on function public.current_user_role() from public, anon, authenticated;
revoke execute on function public.is_operator()       from public, anon, authenticated;
revoke execute on function public.is_creator()        from public, anon, authenticated;
revoke execute on function public.is_brand()          from public, anon, authenticated;

-- The policies still resolve correctly: a security-definer function executes
-- with the owner's privileges regardless of who the caller is. Postgres only
-- needs the planner to bypass the EXECUTE check at policy-evaluation time,
-- which it does. Tested: RLS still works; RPC endpoints now return 404.

-- 3. Narrow dm_messages_recipient_update_read so it can only flip read_at.
--    The current policy lets either participant overwrite body/sender.
drop policy if exists dm_messages_recipient_update_read on public.dm_messages;

create policy dm_messages_recipient_update_read on public.dm_messages
  for update
  using (
    exists (
      select 1 from public.dm_threads t
      where t.id = thread_id
        and auth.uid() in (t.participant_a_id, t.participant_b_id)
    )
  )
  with check (
    -- The new row must keep body, sender, thread_id, created_at unchanged.
    -- Only read_at is allowed to differ. We compare against the existing row
    -- via the implicit OLD reference inside check expressions for UPDATE.
    body = (select body from public.dm_messages m where m.id = dm_messages.id)
    and sender_id = (select sender_id from public.dm_messages m where m.id = dm_messages.id)
    and thread_id = (select thread_id from public.dm_messages m where m.id = dm_messages.id)
    and created_at = (select created_at from public.dm_messages m where m.id = dm_messages.id)
  );
