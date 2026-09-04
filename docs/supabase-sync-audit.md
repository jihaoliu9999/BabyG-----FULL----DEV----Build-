# babyg supabase sync audit

**date**: 2026-09-04
**mode**: read-only. no schema changes, no RLS changes, no writes.
**status**: report complete. awaiting approval before any migration is applied.

## bounds of this audit

The session running this audit does **not** have `SUPABASE_URL` /
`SUPABASE_SERVICE_ROLE_KEY` set in its env. That means every
"production" statement below is derived from local files + code
references, not from a live introspection query. To confirm each
finding against actual prod state, run the read-only SQL in
section J against your production database.

---

## A · local migrations (37 files, in order)

| # | file | shape |
|---|---|---|
| 0001 | `extensions_and_helpers.sql` | RPC (`set_updated_at`) |
| 0002 | `schema.sql` | 12 base tables + indexes |
| 0003 | `role_helpers.sql` | RPCs (`current_user_role`, `is_operator`, `is_creator`, `is_brand`) |
| 0004 | `rls_policies.sql` | RLS enable + policies |
| 0005 | `security_hardening.sql` | RPC exec grants, dm_messages policy fix |
| 0006 | `audit_fixes.sql` | brand_profiles + intel_posts fixes |
| 0007 | `remove_brand_side.sql` | drops brand_profiles + notification alter |
| 0008 | `lint_fixes.sql` | grants |
| 0009 | `oauth_connections.sql` | new table + RLS |
| 0010 | `profile_photos_bucket.sql` | storage bucket `profile-photos` |
| 0011 | `oauth_connections_provider_relax.sql` | alters |
| 0012 | `action_proposals.sql` | new table + RLS |
| 0013 | **`creator_discovery_actions.sql`** | new table + RLS |
| 0013 | **`creator_location_fields.sql`** | 5 cols added to creator_profiles |
| 0014 | `undo_pass_and_disconnect.sql` | alters |
| 0015 | `restore_brand_profiles.sql` | re-creates brand_profiles (reverses 0007) |
| 0016 | `creator_prefs.sql` | 6 cols added to creator_profiles |
| 0017 | `creator_deal_prefs.sql` | deal-pref cols added to creator_profiles |
| 0018 | `applied_migrations_rpc.sql` | RPC `applied_migration_names()` |
| 0019 | `opportunity_cards.sql` | alter creator_job_listings |
| 0020 | `discovery_card_view.sql` | view `discovery_cards` |
| 0021 | `mixed_discovery_actions.sql` | alter creator_discovery_actions |
| 0022 | `dm_ai_briefs.sql` | new table + RLS |
| 0023 | `brand_trust.sql` | brand_profiles cols + brand_trust_checks table |
| 0024 | `new_table_policy_hardening.sql` | policy tightening |
| 0025 | `brand_email_domain_sync.sql` | trigger function |
| 0026 | `dm_ai_brief_upgrade.sql` | dm_ai_briefs cols |
| 0027 | `bot_turns.sql` | new table + RLS |
| 0028 | `babyg_memory_core.sql` | 4 memory tables |
| 0029 | `babyg_memory_deals.sql` | deals + touchpoints tables |
| 0030 | `babyg_memory_relations.sql` | relationship_notes table |
| 0031 | `memory_access_audit.sql` | audit table (service-role only) |
| 0032 | `bot_job_infra.sql` | bot_job_runs + bot_job_failures |
| 0033 | `instagram_metrics_daily.sql` | new table + RLS |
| 0034 | `babyg_agent_autonomy.sql` | 3 cols added to creator_profiles |
| 0035 | `agent_daily_spend.sql` | new table + RLS |
| 0036 | `agent_cycles.sql` | new table + RLS |
| 0037 | `creator_agent_memory.sql` | memory + history table |

**44 unique table names** created across these files.

### issues in the local set

- **duplicate `0013_` prefix**: `0013_creator_discovery_actions.sql` and `0013_creator_location_fields.sql`. Both are separately valid, touch different objects (one creates a table, the other adds columns to creator_profiles), and both will be recorded under their full basename in `supabase_migrations.schema_migrations`. Cosmetic-only issue; no actual DDL conflict. Fixing means renaming one file to 0013a / 0013b or bumping to 0014a, which would require re-registering. **Low priority; leave alone until you touch either migration for another reason.**

- **no `supabase/migrations/` directory**. Everything lives in `migrations/` at repo root. Confirms the app's `check_migration_drift` in `app/core/migration_check.py` uses the right directory.

---

## B · production applied migrations

**Cannot introspect from this session** (no credentials in env). To
retrieve, run against prod:

```sql
select name from supabase_migrations.schema_migrations order by name;
```

Expected list (if all local migrations applied cleanly): every file
stem in section A, i.e. `0001_extensions_and_helpers` through
`0037_creator_agent_memory`, with **two rows for 0013**
(`0013_creator_discovery_actions` + `0013_creator_location_fields`).

The `applied_migration_names()` RPC (migration 0018) also exposes
this to the app at boot. `app/core/migration_check.py` compares
local stems against the RPC output — that's already how babyg logs
migration drift on boot in non-strict mode.

---

## C · gaps / mismatches (expected pre-audit)

Every finding here is an **expected discrepancy** based on the code
being newer than production could be. Verify each with SQL in
section J.

1. **0034_babyg_agent_autonomy** (new this session): adds
   `babyg_agent_internal_actions`, `babyg_agent_gmail_auto_send`,
   `babyg_agent_calendar_holds` to `creator_profiles`.
   - Code in `app/services/agent_autonomy.py`, `app/routes/creator.py`
     (profile_babyg_update route), `app/templates/creator/profile_settings.html`
     depend on these columns.
   - If **not applied**: settings page POST silently writes to
     non-existent columns → PostgREST returns 400 → user sees
     "couldn't save. try again." flash. Agent autonomy gate uses
     `.get(col)` with defaults, so agent still works but every
     creator falls to default-off external autonomy.

2. **0035_agent_daily_spend** (new): creates `agent_daily_spend`.
   - Code in `app/services/agent_cost.py`, `app/services/babyg_agent_loop.py`.
   - If not applied: agent loop's cost-cap read returns 0 (row
     doesn't exist), agent runs unbounded until first write attempt,
     which fails silently (broad except). **Real risk.**

3. **0036_agent_cycles** (new): creates `agent_cycles`.
   - Code in `app/services/agent_cycles.py`, `app/services/babyg_agent_loop.py`,
     `app/services/agent_recap.py`.
   - If not applied: every cycle's trace record silently fails. Loop
     still runs (the record write is best-effort), but there's no
     audit trail. Recap card on home reads 0 cycles → recap card
     hides. **Observability-only impact.**

4. **0037_creator_agent_memory** (new): creates `creator_agent_memory`
   + `creator_agent_memory_history`.
   - Code in `app/services/agent_memory.py`, `app/routes/creator.py`
     (profile_settings_page loads memory; profile_babyg_memory_update
     route writes it).
   - If not applied: settings "what babyg knows" panel shows empty
     textarea forever; save POST → PostgREST 404 → flash `memory=save_failed`.
     Agent memory writes silently no-op.

None of the pre-0034 migrations are new to production. Migrations
0009–0033 have shipped in earlier deploys; drift there is unlikely
but should be spot-checked (section J).

---

## D · table-by-table status (code vs schema)

### tables both defined in migrations AND actively referenced by code

| table | code refs | RLS | indexes | notes |
|---|---:|---|---:|---|
| users | 7 | ✅ | (pk) | core |
| creator_profiles | 4 | ✅ | 2 | 3 alter migrations (0013b, 0016, 0017, 0034) — all cols verified referenced |
| brand_profiles | 9 | ✅ | 2 | dropped in 0007, restored in 0015 |
| oauth_connections | 10 | ✅ | 1 | google + instagram tokens |
| action_proposals | 6 | ✅ | 2 | pending queue for gmail draft / calendar events |
| bot_messages | 6 | ✅ | 2 | agent nudges land here |
| bot_turns | 2 | ✅ | 3 | observability writes; broad-except-wrapped |
| bot_job_runs | 2 | ✅ | 2 | sweep dedupe log |
| bot_job_failures | 1 | ✅ | 1 | sweep failure log |
| dm_threads | 7 | ✅ | 2 | site-native DMs (NOT instagram DMs) |
| dm_messages | 7 | ✅ | 1 | site-native DMs |
| dm_ai_briefs | 5 | ✅ | 1 | dm brief cache |
| creator_connections | 8 | ✅ | — | pending/accepted graph |
| creator_discovery_actions | 7 | ✅ | 3 | swipe history |
| creator_job_listings | 11 | ✅ | 2 | opportunity cards |
| notifications | 6 | ✅ | (pk) | in-app alerts |
| content_receipts | 2 | ✅ | — | creator content log |
| content_reminders | 1 | ✅ | — | |
| performance_logs | 1 | ✅ | — | |
| profile_views | 3 | ✅ | — | |
| bookings | 5 | ✅ | — | |
| abuse_reports | 6 | ✅ | — | |
| audit_log | 2 | ✅ | — | |
| operator_notes | 2 | ✅ | — | |
| intel_posts | 6 | ✅ | 3 | |
| babyg_memory_drafts | 5 | ✅ | 4 | drafts hosted by babyg |
| babyg_memory_deals | 11 | ✅ | 3 | brand deals |
| babyg_memory_deal_touchpoints | 2 | ✅ | 2 | deal history |
| babyg_memory_relationship_notes | 2 | ✅ | 3 | |
| babyg_memory_decisions | 1 | ✅ | 2 | dispatched via babyg_memory.py map |
| babyg_memory_voice_samples | 1 | ✅ | 2 | dispatched via map |
| babyg_memory_creator_preferences | 1 | ✅ | 1 | dispatched via map |
| babyg_memory_contract_flags | 1 | ✅ | — | referenced in map only — verify actual reads |
| memory_access_audit | 1 | ✅ | 2 | service-role only, correct |
| brand_trust_checks | 2 | ✅ | 1 | operator surface |
| discovery_cards | 2 | (view) | — | view over creator_profiles etc. |
| instagram_metrics_daily | 1 | ✅ | 1 | |
| agent_daily_spend | 2 | ✅ | 1 | **new; verify applied** |
| agent_cycles | 3 | ✅ | 2 | **new; verify applied** |
| creator_agent_memory | 2 | ✅ | — | **new; verify applied** |
| creator_agent_memory_history | 3 | ✅ | 1 | **new; verify applied** |

### tables created in migrations but NEVER referenced by code

These are "extra" tables sitting in production with nobody touching
them. Not a sync issue, but a legacy-code footprint.

| table | migration | notes |
|---|---|---|
| `bot_analytics` | 0002 | never wired up |
| `collab_posts` | 0002 | superseded by discover feed |
| `email_drafts` | 0002 | superseded by action_proposals |
| `invite_links` | 0002 | invite flow never shipped |
| `intel_feedback` | 0002 | **just retired via dead-code purge (commit 29b3157). code refs gone; table still in production, empty writes going forward** |

**Not urgent to drop.** Costs almost nothing to leave. When the
codebase gets a real database cleanup pass, these are the first
five to drop.

### tables referenced by code but NOT in local migrations

**None.** Every table the code touches has a matching CREATE TABLE
in `migrations/`.

---

## E · AI V2 readiness

The AI V2 tables the agent loop depends on:

| table | status | risk if not applied |
|---|---|---|
| `bot_messages` | ✅ shipped in 0002 (long ago) | none |
| `bot_turns` | ✅ shipped in 0027 | observability lost |
| `action_proposals` | ✅ shipped in 0012 | gmail-draft path breaks |
| `babyg_memory_drafts` | ✅ shipped in 0028 | drafts unavailable |
| `babyg_memory_deals` | ✅ shipped in 0029 | deal tracking silently fails |
| `babyg_memory_deal_touchpoints` | ✅ shipped in 0029 | touch tracking silently fails |
| `babyg_memory_relationship_notes` | ✅ shipped in 0030 | notes silently fail |
| `memory_access_audit` | ✅ shipped in 0031 | operator audit lost |
| `bot_job_runs` | ✅ shipped in 0032 | sweep dedupe lost — re-runs re-fire nudges |
| `bot_job_failures` | ✅ shipped in 0032 | sweep failure log lost |
| `instagram_metrics_daily` | ✅ shipped in 0033 | ig sweep silently no-ops |
| **`agent_daily_spend`** | ⚠ new in 0035 | **agent runs unbounded until first write fails; verify applied** |
| **`agent_cycles`** | ⚠ new in 0036 | trace log silently drops; recap card hides |
| **`creator_agent_memory`** | ⚠ new in 0037 | memory panel breaks; agent memory writes no-op |
| **`creator_agent_memory_history`** | ⚠ new in 0037 | history panel empty |

Plus the 3 `creator_profiles` autonomy columns from **0034** — if
0034 isn't applied, the settings save silently drops these fields
and every creator gets the migration-default (internal_actions=true,
gmail_auto_send=false, calendar_holds=false), which is safe but
prevents the settings UI from persisting user choices.

**Required for the agent loop to run correctly**:
0034, 0035, 0036 (0037 is optional — the memory-agent tool no-ops
gracefully when the table is missing).

---

## F · RLS / security

**Every table created by any migration has `enable row level
security` in the same migration.** Zero RLS coverage gaps.

Deliberate service-role-only tables (no user-facing SELECT policy —
correct as documented):
- `memory_access_audit` (0031) — operator trust log
- `bot_job_runs`, `bot_job_failures` (0032) — sweep infra
- `instagram_metrics_daily` (0033) — snapshot service
- `agent_daily_spend` (0035) — creators never see raw dollar spend
- `agent_cycles` (0036) — reasoning trace, hidden from creator for
  now (planned to expose later via "why did babyg do X?" surface)

**No cross-user leak risks found** in the audit. Every user-facing
table has a `user_id = auth.uid()` or equivalent self-scoped
policy. `creator_agent_memory` allows owner self-select /
self-upsert / self-update (correct — the creator edits their own
memory from settings).

**One thing to spot-check in production**: `dm_ai_briefs` policies
were replaced in 0024 (`new_table_policy_hardening.sql`). If 0024
didn't apply on top of 0022, older creators may still have the
weaker recipient-select policy. Verify with:

```sql
select policyname, cmd, qual from pg_policies
where schemaname='public' and tablename='dm_ai_briefs';
```

Expect three policies named `dm_ai_briefs_recipient_{select,insert,update}`.

---

## G · index gaps (report only, do not add yet)

Every hot table has at least one composite index covering its
primary access pattern. Nothing screams "missing index" from the
static analysis. Two spots worth watching if production shows
symptoms:

1. **`bot_messages` where role='assistant' AND tool_calls->>'source' LIKE 'agent%'**
   (used by `agent_writes._count_recent_agent_nudges` for the rate
   cap check). Uses `idx_bot_messages_user_created (user_id,
   created_at desc)` which is fine for scanning by user + time; the
   `role` + JSONB filter is a per-row cost after the scan. Fine at
   current scale (per-creator query, low fanout). Revisit if p95
   creeps.

2. **`creator_agent_memory_history` on `updated_by='agent'` filter**
   (agent_recap.py). Uses `idx_creator_agent_memory_history_user_created`
   which covers `user_id + created_at` — the `updated_by` filter is
   a per-row check. Fine for small history counts. Add
   `(user_id, updated_by, created_at)` if a creator's history ever
   exceeds ~1000 rows.

Nothing to change today.

---

## H · silent failure risks (broad try/except around executes)

Every service module wraps its supabase calls in `except Exception:`
by design — the sweeps + agent + observability paths must survive
supabase blips without crashing the caller. The design tradeoff is
that a **missing table looks identical to a supabase hiccup**.

| file | broad-except count | table it wraps | risk if table missing |
|---|---:|---|---|
| `app/services/bot.py` | 26 | bot_messages, babyg_memory_* | bot chat 500s until timeout, then empty state |
| `app/services/babyg_awareness.py` | 22 | multiple (assembly) | awareness snapshot returns empty; bot loses context |
| `app/services/bot_jobs.py` | 14 | bot_job_runs, bot_job_failures | sweep re-fires nudges (no dedupe) |
| `app/services/bot_nudges.py` | 10 | bot_messages | nudges silently drop |
| `app/services/agent_writes.py` | 8 | bot_messages, agent_memory, etc. | agent tools return `{ok: False}` |
| `app/services/babyg_memory.py` | 7 | babyg_memory_* | memory writes no-op |
| `app/services/stats_merge.py` | 6 | performance_logs, instagram_metrics_daily | stats page shows empty |
| `app/services/dm_briefs.py` | 6 | dm_ai_briefs | briefs silently missing |
| `app/services/babyg_deals.py` | 9 | babyg_memory_deals | deal tracking silently no-ops |
| `app/services/discovery.py` | 5 | creator_discovery_actions | swipes lost |
| `app/services/discover.py` | 5 | discovery_cards (view) | discover empty |
| `app/services/agent_tools.py` | 5 | multiple (observation) | agent sees empty world |
| `app/services/agent_recap.py` | 4 | agent_cycles, bot_messages, action_proposals, creator_agent_memory_history | recap card hides |
| `app/services/agent_memory.py` | 4 | creator_agent_memory | memory save returns None |
| `app/services/bot_observability.py` | 3 | bot_turns | observability writes lost |

**Practical rule for spotting drift symptoms**: if a feature "just
renders empty" or "just doesn't save" without a browser console
error, it's overwhelmingly likely the underlying table is missing
or the RLS policy blocks the current user. Check server logs for
`.read_failed` / `.write_failed` warnings from these modules — they
all log the exception.

---

## I · recommended fix plan (with risk level)

Ordered by risk to a live deploy of `main`.

| # | fix | risk | when |
|---|---|---|---|
| 1 | Verify migrations 0034, 0035, 0036, 0037 are applied in production (see section J step 1) | **LOW** — read-only introspection | before flipping `BABYG_AGENT_LOOP_ENABLED=1` |
| 2 | If any of #1 is missing, apply via Supabase dashboard SQL editor or `supabase db push` | **LOW** — every migration uses `create if not exists` + `alter add column if not exists` + `drop policy if exists` before creating; safe to re-run | as soon as #1 is verified |
| 3 | Verify `dm_ai_briefs` has the hardened policies from 0024 (see section J step 3) | **LOW** — read only | anytime |
| 4 | Optional: drop the 5 legacy orphan tables (`bot_analytics`, `collab_posts`, `email_drafts`, `invite_links`, `intel_feedback`) | **MEDIUM** — irreversible; do only if you're certain no external tool queries them | next real cleanup pass, not now |
| 5 | Optional: rename the two `0013_` files to unique prefixes | **VERY LOW** — cosmetic | next time either file is touched for another reason |

---

## J · exact read-only SQL for verification (do not apply)

Run these against **production**. All are read-only. **Nothing here
should be `INSERT` / `UPDATE` / `DELETE` / `DROP` / `ALTER`.**

### 1. is every local migration applied?

```sql
-- Expected: 38 rows (37 files, but two 0013_ share a number).
select name from supabase_migrations.schema_migrations order by name;
```

Compare against local:
```bash
ls migrations/*.sql | xargs -n1 basename | sed 's/\.sql$//' | sort
```

Any name present locally but not in the query result → **needs applying**.
Any name in the query result but not local → informational (one-off
repair or a migration file that was later renamed).

### 2. do the new agent tables exist?

```sql
select table_name from information_schema.tables
where table_schema = 'public'
  and table_name in (
    'agent_daily_spend',
    'agent_cycles',
    'creator_agent_memory',
    'creator_agent_memory_history'
  )
order by table_name;
```

Expected: 4 rows.

### 3. do the new creator_profiles columns exist?

```sql
select column_name, data_type
from information_schema.columns
where table_schema = 'public'
  and table_name = 'creator_profiles'
  and column_name in (
    'babyg_agent_internal_actions',
    'babyg_agent_gmail_auto_send',
    'babyg_agent_calendar_holds'
  )
order by column_name;
```

Expected: 3 rows, all `boolean`.

### 4. verify dm_ai_briefs policy hardening

```sql
select policyname, cmd
from pg_policies
where schemaname = 'public' and tablename = 'dm_ai_briefs'
order by policyname;
```

Expected: 3 rows named `dm_ai_briefs_recipient_select`,
`dm_ai_briefs_recipient_insert`, `dm_ai_briefs_recipient_update`.

### 5. verify RLS is on for every user-touched table

```sql
select tablename, rowsecurity
from pg_tables
where schemaname = 'public'
  and tablename in (
    'action_proposals', 'agent_cycles', 'agent_daily_spend',
    'bot_messages', 'bot_turns', 'creator_agent_memory',
    'creator_agent_memory_history', 'creator_connections',
    'creator_discovery_actions', 'creator_profiles',
    'dm_ai_briefs', 'dm_messages', 'dm_threads',
    'oauth_connections', 'notifications'
  )
order by tablename;
```

Expected: `rowsecurity = t` for every row.

### 6. verify indexes on hot tables

```sql
select tablename, indexname
from pg_indexes
where schemaname = 'public'
  and tablename in (
    'action_proposals', 'agent_cycles', 'agent_daily_spend',
    'bot_messages', 'bot_job_runs', 'creator_agent_memory_history',
    'babyg_memory_deals'
  )
order by tablename, indexname;
```

Compare against the expected list in section G. Any missing index →
non-urgent; queries still work, just slower under load.

### 7. verify the applied_migration_names RPC is callable

```sql
select public.applied_migration_names();
```

Should return a text array. If this errors, migration 0018 didn't
apply and the app's boot-side drift check is silently returning
`available=false` (see `app/core/migration_check.py`).

### 8. verify the profile-photos bucket

```sql
select id, public, file_size_limit, allowed_mime_types
from storage.buckets
where id = 'profile-photos';
```

Expected: `public=true, file_size_limit=6291456,
allowed_mime_types={image/jpeg,image/png,image/webp}`.

---

## K · what must be fixed before more AI/frontend work

**Blocking**:
- Verify migrations 0034 through 0037 applied (section J steps 1–3).
  If any missing, apply before turning on `BABYG_AGENT_LOOP_ENABLED=1`
  in production. Without them the agent loop runs (silently, best-effort)
  but the settings UI, cost cap, and audit trail break.

**Not blocking, verify at leisure**:
- 0024's dm_ai_briefs policy hardening (section J step 4).
- All-tables RLS spot-check (section J step 5).

**Not blocking, defer**:
- Orphan tables (section D). Zero code, zero storage cost worth
  worrying about, zero risk from leaving them alone.
- Duplicate `0013_` prefix. Rename at natural code-touch time.
- Index additions in section G. Add if p95 issues appear.

---

## summary in one line

The code is ahead of what production **might** have applied by
four migrations (0034–0037). Verify with the eight read-only SQL
queries in section J. If any of 0034/0035/0036/0037 haven't
landed, apply them before flipping the agent loop on. Otherwise
you're synced.
