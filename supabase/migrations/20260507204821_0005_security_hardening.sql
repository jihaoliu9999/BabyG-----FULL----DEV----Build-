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

revoke execute on function public.current_user_role() from public, anon, authenticated;
revoke execute on function public.is_operator()       from public, anon, authenticated;
revoke execute on function public.is_creator()        from public, anon, authenticated;
revoke execute on function public.is_brand()          from public, anon, authenticated;

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
    body = (select body from public.dm_messages m where m.id = dm_messages.id)
    and sender_id = (select sender_id from public.dm_messages m where m.id = dm_messages.id)
    and thread_id = (select thread_id from public.dm_messages m where m.id = dm_messages.id)
    and created_at = (select created_at from public.dm_messages m where m.id = dm_messages.id)
  );;
