# Phase 1 → Phase 2 — what's deferred and why

This is the "do not forget" list as we move into Phase 2. Everything
here was reviewed during the Phase 1 final pass; each item is either
genuinely Phase-2-shaped (depends on infrastructure that isn't online
yet) or low enough risk to defer past the closed beta.

If you're picking up Phase 2, start by reading [`AUDIT.md`](./AUDIT.md)
(the fresh-eyes review on the audit branch) — every entry below
references its AUDIT.md / commit origin so you can dig in.

## Carry-overs from the pre-launch audit

### Rate limiter goes Redis (AUDIT M7)

**File:** `app/core/rate_limit.py`.
The token bucket is in-process. Railway's gunicorn boots with
`--workers 2`, so per-IP capacity effectively doubles. The shape of
`magic_link_limiter.allow(...)` was kept compatible with
`slowapi`/`fastapi-limiter` on purpose. Swap when `REDIS_URL` is live.
**When:** as soon as Phase 2 needs Redis for anything (Celery broker,
agent state cache, etc.) — there's no reason to leave this in-process
once we have a shared store.

### Service-role-everywhere → real RLS at the anon client (AUDIT I1)

**Files:** every `app/services/*.py` and `app/core/supabase_client.py`.
Phase 1 talks to Postgres exclusively via the `service_role` key, so
the RLS policies in `migrations/0004_*.sql` and `0005_*.sql` and
`0006_*.sql` are *audited* but never *enforced* on the live path.
Phase 2's per-user agent paths must use a per-user JWT against the
anon client so RLS becomes a second line of defense behind the
route-layer checks. This is a big move — start with one read path
(probably `dms.list_messages`) and measure before going wide.

### CSP `'unsafe-inline'` for styles (AUDIT I3)

**File:** `app/main.py` (CSP header builder). Templates use inline
`style=` attributes in a handful of places (`dm_thread.html`,
`calendar_form.html`, a few partials). Tightening the policy means
moving those into `app/static/css/app.css`. Worth doing before public
launch; not before invite-only beta.

### 30-day session, no idle timeout (AUDIT I2)

`SESSION_MAX_AGE` is 30 days with no inactivity refresh. Standard for
magic-link sites. If product wants stricter (banking-style 30-min
idle), introduce a `last_seen_at` claim on the cookie and refresh on
every authenticated request. Product call, not a security bug.

### Tighten `safe_uuid` to reject the nil UUID

**File:** `app/core/uuid_guard.py`. Currently accepts the all-zero UUID
because no FK in the schema matches it. Phase 2 may introduce nullable
relations defaulting to `00000000-...`; reject it explicitly to remove
the ambiguity.

### Backslash-in-redirect hardening

**File:** `app/core/redirects.py`. `safe_same_origin("/\\evil.com")`
currently returns the string verbatim. Modern browsers don't
normalize `\` to `/`, so it's a dead link — but old clients might.
Add a `\` reject in the first 3 characters.

### Multipart CSRF parser unexercised (AUDIT I5)

**File:** `app/core/csrf.py:_extract_token_from_body`. Phase 1 has no
file-upload forms, so the multipart branch is reachable only by a
fuzzer. When Phase 2 adds uploads (brand-brief PDF, intel-post image,
voice-note ingest), add `tests/test_csrf_multipart.py` and lock the
parser behavior.

### Operator-email timing-oracle jitter is heuristic (M8 final state)

**File:** `app/routes/auth.py:magic_link`. We sleep
`random.uniform(0.15, 0.30)` to equalize with the Supabase OTP RTT.
The actual RTT varies by region and Supabase load; if we ever observe
real timing patterns we should switch to a measured baseline or call
Supabase with a deterministic sentinel email. Not exploitable today.

### N+1 lookup pattern remains in `creator/dm_list` (AUDIT L8)

**File:** `app/routes/creator.py:dm_list`. The peer-lookup loop iterates
threads and falls back from `brands.get_by_user_id` to
`profiles.get_creator_profile` per peer. The single-table
`get_creators_by_ids` helper we just added handles the creator case
once; the brand-side equivalent is a quick `brands.get_by_user_ids`
helper. Defer until Phase 2 introduces denser DM lists.

### `notifications.create` doesn't dedupe (AUDIT L9)

**File:** `app/services/notifications.py`. Two brand-outreach attempts
on the same creator produce two `collab_match` rows. UX-level, easy to
add a `(user_id, kind, link_path)` dedupe later.

### `notifications.create` truncates silently (prior audit)

**File:** `app/services/notifications.py`. Body >2000 chars gets
silently truncated with no log or signal to the caller. Acceptable
for closed beta; add a log line if Phase 2 callers depend on the body
being preserved.

### `views.list_recent_viewers` over-fetches ×4 (AUDIT L4)

**File:** `app/services/views.py`. Pre-`0006_audit_fixes.sql` it had to
dedupe per-day in Python; now the per-day unique index makes the ×4
buffer unnecessary. Quick win, low priority.

## Phase-2 friction points (things to design around now)

### Empty stub packages

`app/agent/`, `app/agent/tools/`, `app/integrations/`, `app/models/`,
`app/tasks/`, `app/services/prompts.py`. Each carries a docstring
explaining the intended shape. When Phase 2 starts:

1. **`app/tasks/`** — create `celery_app.py` exporting a Celery
   instance bound to `settings.celery_broker_url`. Uncomment the
   `worker:` / `beat:` lines in `Procfile`.
2. **`app/integrations/`** — one file per external API. Mirror the
   `app/services/` structure (narrow public interface, errors return
   `None`/`False` rather than raising upstream).
3. **`app/agent/`** — the Claude agent runtime. Pull prompts from
   `app/services/prompts.py` (currently empty, scaffolded with the
   intended phasing of prompts).
4. **`app/models/`** — Pydantic shapes for any agent tool inputs /
   outputs. Phase 1 passes raw dicts; that's fine for HTML routes but
   the agent surface should be typed.

### Config will sprout typed fields

`app/config.py` is intentionally trimmed to just what Phase 1 reads.
When Phase 2 starts reading `ANTHROPIC_API_KEY`, `TAVILY_API_KEY`,
`GOOGLE_*`, etc., add them as typed `BaseSettings` fields. Don't rely
on `extra="ignore"` to keep the unused env vars around — that's just a
transitional accommodation.

### `_assert_session_secret` envelope

**File:** `app/main.py`. Currently enforces secret length for `staging`
and `production`. If we add an `env=preview` for PR-deploys, decide
explicitly: enforce or skip. Don't extend the `dev` short-circuit.

### `dms.list_messages_for_operator` is the privileged door

**File:** `app/services/dms.py`. The participant check on
`list_messages` is now hard-required. Any Phase 2 caller that needs to
read a thread without participant standing (audit log, abuse review,
moderation tooling) MUST go through `list_messages_for_operator` —
which is explicit, greppable, and easy to audit.

### `creators.get_for_view` / `brands.get_for_view` are the public doors

The H2 projection (this branch) routes every cross-user read through
`public_creator` / `public_brand`. Phase 2's JSON endpoints for the
agent MUST use these helpers, not `get_creator_profile` /
`get_by_user_id`. Reviewing a PR? If you see a service call returning
a row directly to a route or agent tool, ask whether the projection
ran.

### `safe_url` Jinja filter

**File:** `app/core/templating.py:_safe_url`. Every `href` that
interpolates a stored URL must be filtered through `|safe_url`. Phase
2 PRs that add new templates should default to this. We can add a
template-time lint pass later (`make lint-templates`).

## Quick-status checklist (current state of the branch)

- ✅ Tests: 302 passing
- ✅ Ruff: clean
- ✅ mypy: clean (44 source files)
- ✅ All HIGH from AUDIT.md closed (H1, H2, H3)
- ✅ MED from AUDIT.md closed (M1, M2, M3, M4, M5, M6, M8, M9) — only M7 deferred (Redis)
- ✅ LOW closed where applicable (L1, L2, L5)
- ✅ CSRF / rate-limit / UUID guard / URL guard / projection — all have regression suites
- ✅ Migration 0006 covers brand_profiles NOT NULL, profile_views per-day index, dm_messages policy
- ⏳ Phase 2 ground prep: above
