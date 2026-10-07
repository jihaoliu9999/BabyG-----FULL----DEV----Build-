-- 0043_bookings_google_unique_full.sql
--
-- INCIDENT REPAIR — Google Calendar sync was fetching every event
-- successfully but persisting zero of them.
--
-- Migration 0041 replaced the original two-column unique constraint
-- ``unique (user_id, google_event_id)`` with a **partial** unique
-- index:
--
--   create unique index bookings_user_google_calendar_event_uidx
--     on public.bookings(user_id, google_calendar_id, google_event_id)
--     where google_event_id is not null;
--
-- PostgreSQL requires the ``ON CONFLICT (columns)`` clause of an
-- ``INSERT ... ON CONFLICT`` (a.k.a. UPSERT) to be matched by either
-- a full unique constraint or a partial unique index whose predicate
-- is repeated verbatim as ``ON CONFLICT ... WHERE <same predicate>``.
-- PostgREST's `.upsert(..., on_conflict="user_id,google_calendar_id,google_event_id")`
-- (see ``app/services/bookings.py::upsert_google_event``) does NOT
-- expose a WHERE hook, so PostgreSQL cannot infer the partial index
-- as the arbiter and raises
--
--   ERROR: there is no unique or exclusion constraint matching
--          the ON CONFLICT specification
--
-- The application catches this as ``PostgrestAPIError`` at bookings.py:137
-- and returns False, which is why every synced Google event was
-- silently skipped and ``imported=0`` was logged.
--
-- Fix: replace the partial unique index with a full unique index on
-- the same three columns. The two are functionally equivalent for
-- our data because ``google_event_id`` is nullable and PostgreSQL
-- treats NULLs as distinct in unique constraints — native babyg
-- bookings (both google columns NULL) never collide with one
-- another under either form, and sync-inserted rows always have
-- both columns set. No existing row can violate the full uniqueness
-- rule that did not already violate the partial one.
--
-- Non-destructive: no rows deleted, no rows modified. Both the
-- drop and the create use ``if [not] exists`` so re-running the
-- migration is safe.

drop index if exists public.bookings_user_google_calendar_event_uidx;

create unique index if not exists bookings_user_google_calendar_event_uidx
  on public.bookings(user_id, google_calendar_id, google_event_id);

comment on index public.bookings_user_google_calendar_event_uidx is
  'Full unique index on the three Google-sync identity columns. NOT partial: PostgREST-driven upsert (`ON CONFLICT (user_id, google_calendar_id, google_event_id)`) requires a full arbiter. See 0043.';
