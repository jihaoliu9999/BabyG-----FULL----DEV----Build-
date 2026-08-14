# Hardening backlog

Deferred items from prior audits. Each is genuinely non-urgent (depends
on infra that isn't online, or product decisions), but should get picked
up as its dependencies land.

## Rate limiter goes Redis

**File:** `app/core/rate_limit.py`.
The token bucket is in-process. Railway's gunicorn boots with
`--workers 2`, so per-IP capacity effectively doubles across workers.
The shape of `magic_link_limiter.allow(...)` was kept compatible with
`slowapi` / `fastapi-limiter` on purpose. Swap when `REDIS_URL` is live
(agent state cache, background jobs, etc.).

## Service-role-everywhere → real RLS at the anon client

**Files:** every `app/services/*.py` and `app/core/supabase_client.py`.
Every server-side read/write goes through the `service_role` key today,
so RLS policies in `migrations/` are *audited* but not *enforced* on the
live path. Per-user agent paths should use a per-user JWT against the
anon client so RLS becomes a second line of defense behind the
route-layer checks. Big move — start with one read path (e.g.
`dms.list_messages`) and measure before going wide.

## CSP `'unsafe-inline'` for styles

**File:** `app/main.py` (CSP header builder). Templates use inline
`style=` attributes in a handful of places (`dm_thread.html`,
`calendar_form.html`, a few partials). Tightening the policy means
moving those into `app/static/css/app.css`. Worth doing before public
launch.

## 30-day session, no idle timeout

`SESSION_MAX_AGE` is 30 days with no inactivity refresh. Standard for
magic-link sites. If product wants stricter (banking-style 30-min idle),
introduce a `last_seen_at` claim on the cookie and refresh on every
authenticated request. Product call, not a security bug.

## Tighten `safe_uuid` to reject the nil UUID

**File:** `app/core/uuid_guard.py`. Currently accepts the all-zero UUID
because no FK in the schema matches it. Reject explicitly to remove the
ambiguity before any nullable relation defaults to `00000000-...`.

## Backslash-in-redirect hardening

**File:** `app/core/redirects.py`. `safe_same_origin("/\\evil.com")`
currently returns the string verbatim. Modern browsers don't normalize
`\` to `/`, so it's a dead link — but old clients might. Add a `\`
reject in the first 3 characters.

## Operator-email timing-oracle jitter is heuristic

**File:** `app/routes/auth.py:magic_link`. We sleep
`random.uniform(0.15, 0.30)` to equalize with the Supabase OTP RTT. The
actual RTT varies by region and Supabase load; if we ever observe real
timing patterns we should switch to a measured baseline or call Supabase
with a deterministic sentinel email. Not exploitable today.

## N+1 in `creator/dm_list`

**File:** `app/routes/creator.py:dm_list`. The peer-lookup loop falls
back from `brands.get_by_user_id` to `profiles.get_creator_profile` per
peer. The single-table `get_creators_by_ids` helper handles the creator
case once; add a `brands.get_by_user_ids` equivalent when DM lists get
denser.

## Notifications: dedupe + truncation logging

**File:** `app/services/notifications.py`. Two brand-outreach attempts
on the same creator produce two `collab_match` rows — add a
`(user_id, kind, link_path)` dedupe. Separately, `body > 2000` chars
silently truncates; add a log line if callers ever depend on the body
being preserved.

## `views.list_recent_viewers` over-fetches ×4

**File:** `app/services/views.py`. Pre-`0006_audit_fixes.sql` it had to
dedupe per-day in Python; the per-day unique index makes the ×4 buffer
unnecessary now.

## Design invariants worth keeping

- **`dms.list_messages_for_operator` is the privileged door.** Any
  caller that needs to read a thread without participant standing
  (audit log, abuse review, moderation tooling) MUST go through this
  explicit function — greppable, easy to audit.
- **`get_for_view` is the public projection door.** Cross-user reads go
  through `public_creator` / `public_brand`, not raw
  `get_creator_profile` / `get_by_user_id`.
- **`safe_url` Jinja filter.** Every `href` that interpolates a stored
  URL must be filtered through `|safe_url`.
- **`app/services/prompts.py`** is the single home for LLM prompts. No
  prompts elsewhere.
