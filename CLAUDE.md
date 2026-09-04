# babyg working rules

## supabase sync — always verify after migration-touching pushes

Whenever a session pushes code that:
- adds a new file to `migrations/`, or
- adds a column/table/RLS policy/RPC that a code path reads or writes, or
- deletes any of the above,

the session **must** end with a supabase sync check before declaring
the work done. The check is:

1. Read `docs/supabase-sync-audit.md` § J for the read-only SQL
   templates already written for this repo (migration registry,
   column check, RLS spot-check, RPC callable check, bucket check).
2. If `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY` are in the session
   env, run the four queries directly. Otherwise hand the queries
   back to the user to paste into the Supabase SQL editor.
3. Report **explicitly** which of the newly-pushed migrations show
   up in `supabase_migrations.schema_migrations` and which don't.
4. **Do not** claim the work is "shipped" or "deployed" until either
   (a) the migration is confirmed applied in production, or
   (b) the user has explicitly accepted the code-ahead-of-schema
   state (e.g. "I'll apply it later, keep going").

Any commit that fails this check is code-only landed, not
production-ready. Say so clearly in the wrap-up message.

## constraints on schema changes

- Never DROP, TRUNCATE, or DELETE production data or objects without
  a signed-off SQL snippet from the user.
- Never touch RLS policies on existing tables (with data in them)
  without explicit confirmation.
- Every migration file uses `create if not exists` / `add column if
  not exists` / `drop policy if exists` before create, so re-running
  an applied migration is safe. New migrations must follow the same
  pattern.
- Never use the service role to bypass RLS in a way that would hide
  a missing policy from production behavior.

## repo layout facts

- Migrations live at `migrations/` (root), NOT `supabase/migrations/`.
- `app/core/migration_check.py` calls the `applied_migration_names()`
  RPC (migration 0018) at boot; non-strict mode runs on a background
  daemon thread and logs warnings.
- The service-role supabase client is `app.core.supabase_client.get_service_client()`.
  It raises RuntimeError if env is missing — never catch that to
  fake a missing client.

## push discipline

- Every commit ends by pushing to both `claude/code-review-improvements-wNT01`
  (dev) and `main`. The dev branch stays fast-forwardable with main.
- Sentry is wired but no-ops without `SENTRY_DSN`. Errors are still
  logged.
- The background agent loop is feature-flagged behind
  `BABYG_AGENT_LOOP_ENABLED`. Never turn it on for a user without
  first confirming migrations 0034–0037 are applied in production.
