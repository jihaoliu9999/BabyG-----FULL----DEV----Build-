# Migrations

Supabase Postgres schema and Row-Level Security policies for babyg.

Files are applied in numeric order. Each migration is plain SQL — run via the Supabase MCP `apply_migration` tool, the Supabase CLI, or pasted into the SQL editor.

| File | Purpose |
| --- | --- |
| _to be added in Phase 1 Step 2_ | Initial schema (all tables from §10.2 of the deck) |
| _to be added in Phase 1 Step 2_ | RLS policies for every table |

Conventions:

- Lowercase `snake_case` table and column names.
- Every table has `id uuid primary key default gen_random_uuid()`, `created_at timestamptz default now()`, and `updated_at timestamptz default now()` unless explicitly noted.
- RLS is enabled on every table. No table is queryable without auth.
- The `service_role` key bypasses RLS — used only in Celery worker tasks and trusted server paths.
