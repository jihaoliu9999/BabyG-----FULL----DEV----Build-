-- babyg :: 0024 :: scope new-table policies to authenticated users

drop policy if exists dm_ai_briefs_recipient_select on public.dm_ai_briefs;
drop policy if exists dm_ai_briefs_recipient_insert on public.dm_ai_briefs;
drop policy if exists dm_ai_briefs_recipient_update on public.dm_ai_briefs;

create policy dm_ai_briefs_recipient_select
  on public.dm_ai_briefs
  for select to authenticated
  using (recipient_user_id = auth.uid() or public.is_operator());

create policy dm_ai_briefs_recipient_insert
  on public.dm_ai_briefs
  for insert to authenticated
  with check (recipient_user_id = auth.uid() or public.is_operator());

create policy dm_ai_briefs_recipient_update
  on public.dm_ai_briefs
  for update to authenticated
  using (recipient_user_id = auth.uid() or public.is_operator())
  with check (recipient_user_id = auth.uid() or public.is_operator());

revoke all on public.dm_ai_briefs from anon;
grant select, insert, update on public.dm_ai_briefs to authenticated, service_role;

drop policy if exists brand_trust_checks_operator_select on public.brand_trust_checks;
drop policy if exists brand_trust_checks_operator_write on public.brand_trust_checks;
drop policy if exists brand_trust_checks_operator_all on public.brand_trust_checks;

create policy brand_trust_checks_operator_all
  on public.brand_trust_checks
  for all to authenticated
  using (public.is_operator()) with check (public.is_operator());

revoke all on public.brand_trust_checks from anon, authenticated;
grant select, insert, update, delete on public.brand_trust_checks to service_role;;
