# Migrations

Supabase Postgres schema and Row-Level Security policies for babyg.

Files are applied in numeric order. Each migration is plain SQL — run via the Supabase MCP `apply_migration` tool, the Supabase CLI (`supabase db push`), or pasted into the SQL editor.

| File | Purpose |
| --- | --- |
| `0001_extensions_and_helpers.sql` | `pgcrypto`, `citext`, shared `set_updated_at` trigger |
| `0002_schema.sql` | All 22 tables: identity, intel, bot/receipts, calendar, network, safety, notifications |
| `0003_role_helpers.sql` | Role helper functions (`current_user_role`, `is_operator`, `is_creator`, `is_brand`) — must run after schema since their bodies reference `public.users` |
| `0004_rls_policies.sql` | Enables RLS on every table and writes policies per product manual §10.2 |
| `0005_security_hardening.sql` | Pins `set_updated_at` search_path; revokes EXECUTE on RLS helper fns from anon/authenticated so they cannot be called via `/rest/v1/rpc/`; narrows `dm_messages` UPDATE policy so participants can only flip read state |
| `0006_audit_fixes.sql` | Drops the brittle `dm_messages_recipient_update_read` policy from 0005; tightens `brand_profiles.contact_full_name` to NOT NULL; adds per-day unique index on `profile_views (viewer_id, viewed_id, date(viewed_at))` so reloads don't inflate counts; rewrites `intel_posts.target_tiers` default with quoted array literal |

## Conventions

- Lowercase `snake_case` for tables and columns.
- Every table uses `id uuid primary key default gen_random_uuid()` unless it 1:1-mirrors `auth.users` (then `id uuid references auth.users(id)`).
- `created_at` / `updated_at` are `timestamptz default now()`. The `updated_at` trigger is wired on tables that mutate.
- RLS is enabled on every public table. The `service_role` key bypasses RLS — used only by Celery workers and trusted server paths.
- Tier gating (e.g. profile-viewer visibility for VIP) is enforced in the API layer, not RLS.

## Calendar

Google Calendar is the source of truth for events. `bookings.google_event_id` and `bookings.google_calendar_id` are the back-references; babyg metadata (type tag, intel link, brand link, status, venue refs) is persisted locally.

## Applying

Once a Supabase project is provisioned and `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` are set:

```bash
supabase link --project-ref <ref>
supabase db push
```

Or apply each file via the Supabase MCP `apply_migration` tool against the target project.
