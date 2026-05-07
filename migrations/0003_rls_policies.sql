-- babyg :: 0003 :: row-level security
-- Enables RLS on every public table and writes the policies described in
-- the product manual §10.2. Operators bypass via public.is_operator().
-- The Supabase service_role key bypasses RLS entirely and is used only by
-- the Celery worker and trusted server paths.

-- =============================================================================
-- USERS / PROFILES
-- =============================================================================

alter table public.users enable row level security;

create policy users_self_select on public.users
  for select using (id = auth.uid() or public.is_operator());

create policy users_self_update on public.users
  for update using (id = auth.uid() or public.is_operator())
  with check (id = auth.uid() or public.is_operator());

-- Inserts come exclusively from the auth signup trigger or service_role.

alter table public.creator_profiles enable row level security;

create policy creator_profiles_self_select on public.creator_profiles
  for select using (
    user_id = auth.uid()
    or public.is_operator()
    or public.is_creator()                  -- network directory: creators see other creators
    or public.is_brand()                    -- brand directory: brands see creator profiles
  );

create policy creator_profiles_self_write on public.creator_profiles
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

alter table public.brand_profiles enable row level security;

create policy brand_profiles_self_select on public.brand_profiles
  for select using (
    user_id = auth.uid()
    or public.is_operator()
    or public.is_creator()                  -- creators see brand profile when receiving outreach
  );

create policy brand_profiles_self_write on public.brand_profiles
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

alter table public.invite_links enable row level security;

create policy invite_links_operator_only on public.invite_links
  for all using (public.is_operator())
  with check (public.is_operator());

-- =============================================================================
-- INTEL
-- =============================================================================

alter table public.intel_posts enable row level security;

-- Creators read only active, non-archived, non-expired posts. Operators full access.
create policy intel_posts_read_active on public.intel_posts
  for select using (
    public.is_operator()
    or (
      status = 'active'
      and valid_until > now()
      and (
        public.is_creator()
      )
    )
  );

create policy intel_posts_operator_write on public.intel_posts
  for all using (public.is_operator())
  with check (public.is_operator());

alter table public.intel_feedback enable row level security;

create policy intel_feedback_self on public.intel_feedback
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

alter table public.collab_posts enable row level security;

create policy collab_posts_read_active on public.collab_posts
  for select using (
    public.is_operator()
    or (status = 'active' and valid_until > now() and public.is_creator())
  );

create policy collab_posts_operator_write on public.collab_posts
  for all using (public.is_operator())
  with check (public.is_operator());

-- =============================================================================
-- BOT / RECEIPTS / ANALYTICS / DRAFTS
-- =============================================================================

alter table public.bot_messages enable row level security;

-- Creators read their own. Operators read all. Only operators may flip flag fields.
create policy bot_messages_self_select on public.bot_messages
  for select using (user_id = auth.uid() or public.is_operator());

create policy bot_messages_self_insert on public.bot_messages
  for insert with check (user_id = auth.uid() or public.is_operator());

create policy bot_messages_operator_update on public.bot_messages
  for update using (public.is_operator())
  with check (public.is_operator());

alter table public.content_receipts enable row level security;

create policy content_receipts_self on public.content_receipts
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

alter table public.bot_analytics enable row level security;

create policy bot_analytics_operator_only on public.bot_analytics
  for select using (public.is_operator());

alter table public.email_drafts enable row level security;

create policy email_drafts_self on public.email_drafts
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

-- =============================================================================
-- CALENDAR
-- =============================================================================

alter table public.bookings enable row level security;

create policy bookings_self on public.bookings
  for all using (user_id = auth.uid() or public.is_operator())
  with check (user_id = auth.uid() or public.is_operator());

alter table public.content_reminders enable row level security;

-- Creators read their own. Writes happen via service_role from Celery.
create policy content_reminders_self_select on public.content_reminders
  for select using (user_id = auth.uid() or public.is_operator());

-- =============================================================================
-- NETWORK
-- =============================================================================

alter table public.creator_connections enable row level security;

create policy creator_connections_participant on public.creator_connections
  for select using (
    requester_id = auth.uid()
    or addressee_id = auth.uid()
    or public.is_operator()
  );

create policy creator_connections_participant_write on public.creator_connections
  for all using (
    requester_id = auth.uid()
    or addressee_id = auth.uid()
    or public.is_operator()
  )
  with check (
    requester_id = auth.uid()
    or addressee_id = auth.uid()
    or public.is_operator()
  );

alter table public.creator_job_listings enable row level security;

create policy creator_job_listings_read_active on public.creator_job_listings
  for select using (
    public.is_operator()
    or (is_active = true and is_taken_down = false)
    or poster_user_id = auth.uid()
  );

create policy creator_job_listings_poster_write on public.creator_job_listings
  for insert with check (poster_user_id = auth.uid() or public.is_operator());

create policy creator_job_listings_poster_update on public.creator_job_listings
  for update using (poster_user_id = auth.uid() or public.is_operator())
  with check (poster_user_id = auth.uid() or public.is_operator());

create policy creator_job_listings_poster_delete on public.creator_job_listings
  for delete using (poster_user_id = auth.uid() or public.is_operator());

alter table public.profile_views enable row level security;

-- Viewers may write their own view rows. The viewee can read their incoming
-- views; the API layer applies tier gating before exposing the data.
create policy profile_views_viewer_insert on public.profile_views
  for insert with check (viewer_id = auth.uid());

create policy profile_views_viewee_read on public.profile_views
  for select using (
    viewed_id = auth.uid()
    or viewer_id = auth.uid()
    or public.is_operator()
  );

alter table public.dm_threads enable row level security;

create policy dm_threads_participant on public.dm_threads
  for select using (
    auth.uid() in (participant_a_id, participant_b_id)
    or public.is_operator()
  );

create policy dm_threads_participant_write on public.dm_threads
  for insert with check (auth.uid() in (participant_a_id, participant_b_id));

create policy dm_threads_participant_update on public.dm_threads
  for update using (auth.uid() in (participant_a_id, participant_b_id))
  with check (auth.uid() in (participant_a_id, participant_b_id));

alter table public.dm_messages enable row level security;

create policy dm_messages_participant_select on public.dm_messages
  for select using (
    public.is_operator()
    or exists (
      select 1 from public.dm_threads t
      where t.id = thread_id
        and auth.uid() in (t.participant_a_id, t.participant_b_id)
    )
  );

create policy dm_messages_sender_insert on public.dm_messages
  for insert with check (
    sender_id = auth.uid()
    and exists (
      select 1 from public.dm_threads t
      where t.id = thread_id
        and auth.uid() in (t.participant_a_id, t.participant_b_id)
    )
  );

create policy dm_messages_recipient_update_read on public.dm_messages
  for update using (
    exists (
      select 1 from public.dm_threads t
      where t.id = thread_id
        and auth.uid() in (t.participant_a_id, t.participant_b_id)
    )
  )
  with check (true);

-- =============================================================================
-- SAFETY / MODERATION / OPS
-- =============================================================================

alter table public.abuse_reports enable row level security;

create policy abuse_reports_reporter_select on public.abuse_reports
  for select using (reporter_id = auth.uid() or public.is_operator());

create policy abuse_reports_reporter_insert on public.abuse_reports
  for insert with check (reporter_id = auth.uid());

create policy abuse_reports_operator_update on public.abuse_reports
  for update using (public.is_operator())
  with check (public.is_operator());

alter table public.operator_notes enable row level security;

create policy operator_notes_operator_only on public.operator_notes
  for all using (public.is_operator())
  with check (public.is_operator());

alter table public.performance_logs enable row level security;

create policy performance_logs_self_select on public.performance_logs
  for select using (user_id = auth.uid() or public.is_operator());

create policy performance_logs_operator_write on public.performance_logs
  for insert with check (public.is_operator());

create policy performance_logs_operator_update on public.performance_logs
  for update using (public.is_operator())
  with check (public.is_operator());

alter table public.audit_log enable row level security;

create policy audit_log_operator_only on public.audit_log
  for select using (public.is_operator());
-- inserts go through service_role

-- =============================================================================
-- NOTIFICATIONS
-- =============================================================================

alter table public.notifications enable row level security;

create policy notifications_self_select on public.notifications
  for select using (user_id = auth.uid() or public.is_operator());

create policy notifications_self_update on public.notifications
  for update using (user_id = auth.uid())
  with check (user_id = auth.uid());
