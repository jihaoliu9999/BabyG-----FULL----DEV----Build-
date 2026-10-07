-- 0050_creator_job_offer_responses.sql
--
-- Step 6B: the recipient (applicant) of an offer can view it and then
-- Accept or Decline it. Smallest extension of creator_job_offers (0049):
--
--   * viewed_at     -- set when the recipient first opens the offer; an
--                      offer is "unread" while status = 'sent' and
--                      viewed_at is null.
--   * responded_at  -- set once, together with the final status.
--   * status        -- widened from ('sent') to ('sent', 'accepted',
--                      'declined'). No other lifecycle states.
--
-- Privacy posture is unchanged: RLS stays enabled with no policies and
-- anon/authenticated keep no access. The service role gains UPDATE on
-- exactly the three lifecycle columns; it already had SELECT, INSERT.
--
-- Additive and non-destructive: no table, column or row is dropped or
-- rewritten. The only replaced object is the status CHECK, which Postgres
-- can only widen by drop+add; both happen inside one transaction, so
-- there is no window without the constraint, and every existing row
-- ('sent', responded_at null) satisfies the new checks. Safe to re-run.
begin;

alter table public.creator_job_offers
  add column if not exists viewed_at timestamptz;

alter table public.creator_job_offers
  add column if not exists responded_at timestamptz;

alter table public.creator_job_offers
  drop constraint if exists creator_job_offers_status_check;
alter table public.creator_job_offers
  add constraint creator_job_offers_status_check
  check (status in ('sent', 'accepted', 'declined'));

-- A decision always carries its timestamp, and only a decision does.
alter table public.creator_job_offers
  drop constraint if exists creator_job_offers_response_consistency;
alter table public.creator_job_offers
  add constraint creator_job_offers_response_consistency
  check (
    (status = 'sent' and responded_at is null)
    or (status in ('accepted', 'declined') and responded_at is not null)
  );

-- Received-offers inbox + unread badge: always filtered by recipient.
create index if not exists creator_job_offers_applicant_idx
  on public.creator_job_offers (applicant_user_id, created_at desc);

grant update (status, viewed_at, responded_at)
  on public.creator_job_offers to service_role;

comment on column public.creator_job_offers.viewed_at is
  'Step 6B: first time the recipient opened this offer. Unread = status sent and viewed_at null.';
comment on column public.creator_job_offers.responded_at is
  'Step 6B: when the recipient accepted or declined. Set exactly once with the final status.';
comment on column public.creator_job_offers.status is
  'sent -> accepted | declined (final). Step 6B emits only these three; later steps may widen the CHECK.';

commit;
