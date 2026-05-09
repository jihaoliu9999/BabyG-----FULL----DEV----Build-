# Phase 1 audit — fresh eyes pass

Read of every line shipped on `claude/phase-1-code-audit-kZlHH` after
your partner's audit + my CI fix. **Goal: identify everything still
worth fixing before real users sign in.**

What I read: 6.3K LOC across `app/` (43 modules), 5.1K LOC of tests,
6 SQL migrations, 51 Jinja templates. Cross-checked services against
their callers and against the schema.

**Not reviewed because they're empty stubs:** `app/agent/`,
`app/agent/tools/`, `app/integrations/`, `app/models/`, `app/tasks/`,
`app/services/prompts.py`. These are Phase 2/3 placeholders.

---

## Severity legend

- **CRIT** — exploitable now; ship a fix before any user signs in
- **HIGH** — exploitable but requires a specific path; ship before public launch
- **MED** — defense-in-depth; bug surface that's not directly exploitable today
- **LOW** — code quality / tidy-up
- **INFO** — observation, not a bug

---

## What's already good

So I don't bury the lede: a lot of structural risk is already closed.

- CSRF middleware, security headers, GZip, cached static — all wired.
- Rate-limit on `/auth/magic-link` (5 / 10 min per-IP).
- `client_ip` reads the LAST `X-Forwarded-For` entry (correct for one proxy hop).
- Open-redirect closed (`safe_same_origin` on `/report` and notifications).
- `dms.list_messages` accepts `participant_id` for defense-in-depth.
- HTML 401 → login redirect; CSRF rejection content-negotiated (HTML vs JSON).
- Session-secret guard refuses to boot in prod with weak/default value.
- `_assert_session_secret`, idempotent auth-callback upserts, audit trail.
- Per-day unique index on `profile_views` (UTC-pinned, immutable expression).
- HSTS / X-Frame-Options DENY / nosniff / minimal CSP.

The base is solid. What follows is what's still loose.

---

## CRIT — none

I went looking. Nothing in the current code is "exploitable today by a
random signed-in user." The remaining issues are MED at most or
require a future code path to introduce.

---

## HIGH

### H1 · Brand `brand_website` and similar URL fields aren't scheme-validated → stored XSS via `javascript:` URLs

**Files:** `app/routes/onboarding.py:_validate_brand` (no scheme check on
`brand_website`); `app/routes/operator.py:_validate_intel` (no scheme
check on `source`); `app/routes/creator.py:_validate_receipt` (no
scheme check on `post_url`).

**Templates rendering them:**
- `app/templates/operator/brand_detail.html:18`
- `app/templates/creator/brand_view.html:14`
- `app/templates/_partials/intel_card.html:18`
- `app/templates/creator/receipts_list.html:45`

**The bug:** Jinja autoescape protects against attribute-quote escapes
(`"` `<` `>`) but does NOT block the `javascript:` URL scheme. A
malicious brand can submit `javascript:fetch('/exfil?'+document.cookie)`
as their website. Every creator clicking the link in `/creator/brand_view.html`
executes it in their session context. The session cookie is HttpOnly, so
the cookie itself is safe — but the attacker can drive the session
(submit forms, post DMs, exfiltrate page contents) for as long as the
link is visited. Confidence on real-world exploitability: HIGH for
brand→creator, MED for receipts (self-only, but still).

**Why it's HIGH not CRIT:** brand verification is a manual operator step,
so a malicious URL has to get past your eyeball first. Operator
verification UX should already render the URL link — they'd see the
weird scheme. Acceptable risk for closed beta; must close before
public launch.

**Fix:** add an `_http_url(value)` helper that requires `http://` or
`https://` prefix, normalizes, and rejects everything else.
Apply at all three validators. ~15 lines.

---

### H2 · `creators.get_for_view` and `network.list_directory_for_creator` return `SELECT *` → privacy leak surface

**Files:** `app/services/creators.py:_list_onboarded_creators`,
`app/services/creators.py:get_for_view`, `app/services/network.py:_list_onboarded_creators`,
`app/services/profiles.py:_get_profile`.

**Today's templates render only safe fields** (full_name, niches,
follower_range, etc.) — so this is *currently* dormant. But the service
contract returns:
- `baseline_followers` — true integer count, vs the discrete band the
  creator actually agreed to share
- `baseline_engagement_rate` — true rate
- `writing_samples` — text the creator drafted privately
- `brand_preferences` — internal preferences
- `notification_settings` (JSON) — internal preferences
- `tier` — `basic`/`pro`/`vip` exposes paid status to other users
- `sub_bot_persona`
- `hard_limits` — privacy-debatable, currently rendered to brands

A future template addition or a json endpoint that does
`return creator` ships the whole row.

**Fix:** project to a `_PUBLIC_CREATOR_FIELDS` allowlist at the service
boundary. Two functions:

```python
PUBLIC_CREATOR_FIELDS = (
    "user_id", "full_name", "instagram_handle", "primary_platform",
    "neighborhood", "niches", "content_formats", "follower_range",
    "engagement_range", "creator_tenure", "bio",
    # hard_limits debatable — keep if PRD explicitly wants it
)
def _public_creator(row): return {k: row.get(k) for k in PUBLIC_CREATOR_FIELDS}
```

`get_for_view` and `list_directory_for_creator` return `_public_creator(row)`.
Same idea for `brand_profiles.get_by_user_id` when read by a non-owner —
keeps `verification_notes` (which contains operator's private review
notes) server-side only.

---

### H3 · `/report` accepts arbitrary `target_id` → reporter can compel operators to read any DM thread

**File:** `app/routes/abuse.py:report`.

**The flow:** Any creator can `POST /report` with
`target_type=dm_thread` and `target_id=<some_uuid_they_don't_own>`.
The report goes into the queue. Any operator opening
`/operator/abuse/<id>` runs `_abuse_target_context` which calls
`dms.list_messages(target_id, limit=20)` *without* `participant_id`,
returning the full thread.

**Why this matters:** the reporter can essentially weaponize the operator
queue to read DMs they shouldn't see. The operator is supposed to be
trusted, so this is a "social escalation" rather than a tech bypass — but
the report endpoint has no standing check.

**Fix:** in `app/routes/abuse.py:report`, validate reporter standing per
target_type:
- `dm_thread` → reporter must be a participant of the thread
- `profile` → reporter ≠ target user
- `message` → look up message, then thread, then participation
- `listing` → no check (board is public)

If standing fails, return 403 silently (or log + 400).

---

## MED

### M1 · `dms.list_messages` `participant_id` is optional → defense-in-depth fails on caller forget

**File:** `app/services/dms.py:107-127`.

The signature is:
```python
def list_messages(thread_id, *, participant_id=None, limit=200):
    if participant_id is not None and not _is_participant(...):
        return []
```

If a future caller forgets to pass `participant_id`, the participant
check is skipped silently. The original DM-leak class of bug.

**Fix:** make `participant_id` required (no default), or default to a
sentinel that means "no check, internal use only" (e.g. `participant_id="*"`)
and use that value at the one operator-side call site that legitimately
needs to bypass the check (`_abuse_target_context`).

---

### M2 · `dms.send_message` has no participant check on `thread_id` × `sender_id`

**File:** `app/services/dms.py:130-160`.

`send_message(thread_id, sender_id, body)` inserts a message into any
thread for any sender. The route layer always resolves the thread via
`get_or_create_thread(session.user_id, peer)` first, so the canonical
pair guarantees the session user is a participant — that's why it's
not exploitable today. But if a future route ever forwards a
URL-supplied `thread_id` raw, this writes for them.

**Fix:** make `send_message` call `_is_participant(thread_id, sender_id)`
and refuse if False.

---

### M3 · `jobs.update` has no owner filter

**File:** `app/services/jobs.py:113-129`.

Compare to `bookings.update(booking_id, *, user_id, payload)` which
filters `.eq("id", id).eq("user_id", user_id)` so a route-layer slip
can't write across users. `jobs.update` lacks the equivalent
`poster_user_id` filter. The route does the check, but the service
defense is missing.

**Fix:** add `*, poster_id: str` kwarg, filter both `id` and
`poster_user_id`. Update the one caller (`creator/jobs/{id}` POST).
Operators don't write through this — they use `take_down`.

---

### M4 · `/creator/jobs/{id}` shows non-mine **closed** listings to creators

**File:** `app/routes/creator.py:jobs_detail`.

```python
if listing is None or listing.get("is_taken_down"): 404
```

A listing with `is_active=false` (poster soft-closed it) but
`is_taken_down=false` is visible to anyone who guesses the UUID. Brand-
side correctly 404s these:

```python
# brand.py:jobs_detail
if listing is None or listing.get("is_taken_down") or not listing.get("is_active"):
    raise 404
```

**Fix:** mirror the brand check on creator side, but skip the `is_active`
check when `is_mine` (poster should still see their own closed
listings to re-open).

---

### M5 · Anon CSRF cookie expires (30 min) faster than the form session → confusing 403 for slow form-fillers

**File:** `app/core/csrf.py:_wrap_send` (Max-Age=1800).

A signed-out user lands on `/get-started`, opens the magic-link form,
goes to lunch, comes back, hits Submit at minute 31. `bg_csrf` cookie
expired. CSRF middleware `_principal()` sees `'a:none'`, doesn't match
`'a:<old anon>'` baked into the token. 403 with a generic "Request
blocked" page that doesn't suggest "your form expired, refresh."

**Fix:** either (a) extend anon cookie to 24h since the principal
binds nothing privileged, or (b) detect the expiry case and render a
specific "your sign-in form expired, click below" page that just
redirects back to /get-started. Pick (a) — simpler, no UX-text
proliferation.

---

### M6 · `_assert_session_secret` only enforces in `env=production`, not `env=staging`

**File:** `app/main.py:_assert_session_secret`.

```python
if settings.env != "production":
    return
```

Staging deploys can boot with the dev default. If staging is ever
internet-reachable, an attacker who knows the default secret can mint
sessions.

**Fix:** apply the same guard in `staging` (just remove the early-return
for non-production *and* non-dev).

---

### M7 · Per-IP rate limit is in-process → multi-worker on Railway un-rate-limits

**File:** `app/core/rate_limit.py`, `app/main.py` boots gunicorn via
`Procfile`.

`Procfile` uses `gunicorn ... --workers 2` (or whatever value is set).
The token bucket is in-process — each worker has its own bucket, so the
effective per-IP cap is `workers × capacity`.

**Fix:** swap to a Redis-backed limiter for Phase 2. Document the
`workers=1` workaround in `DEPLOY.md` until then. Already noted in
`rate_limit.py`'s docstring; the action item is the doc + maybe a
Railway env override.

---

### M8 · Operator-gate timing equalization is partial

**File:** `app/routes/auth.py:magic_link`.

`_operator_email_authorized` runs unconditionally — good. But the
`should_send` branch is:

- For non-operator role → ALWAYS calls Supabase `sign_in_with_otp`
- For operator role + known operator → calls Supabase
- For operator role + unknown email → does NOT call Supabase

So the request time differs between (operator + known) and (operator +
unknown) by a Supabase round-trip (~100-300 ms). Repeated probing
narrows which emails are operators.

**Fix:** in the negative branch, do an equivalent-cost no-op
(e.g. `await asyncio.sleep(uniform(0.15, 0.30))`), or — better — call
Supabase with a known-bad email so the branches are indistinguishable.
INFO if you don't care about operator-email enumeration.

---

### M9 · `clear_session` on logout doesn't clear `bg_pending_role`

**File:** `app/core/security.py:clear_session`, `app/routes/auth.py:logout`.

Logout clears `bg_session` but leaves `bg_pending_role`. Mostly cosmetic —
the cookie expires after 10 minutes anyway. But signing out and back in
during that window resurrects the previous role hint, which could land
the user on the wrong onboarding flow if they switched.

**Fix:** also call `clear_pending_role(response)` in `/auth/logout`.

---

## LOW

### L1 · `audit.record(notes=...)` accepts unbounded notes

**File:** `app/services/audit.py:record`.

Cap at `[:1000]` to match `abuse.action_notes` and `operator_notes.body`.

---

### L2 · `app/deps.py:_validate_roles` is dead code

Never called. Either wire it as a sanity check on `require_role` boot
or delete.

---

### L3 · `dms.unread_count_for_user` docstring says "single COUNT" but body does two trips

Doc was rewritten in `ccdb740` to acknowledge this, but the
implementation still does two round-trips. Move to a postgres view
when convenient. INFO.

---

### L4 · `app/services/views.py:list_recent_viewers` over-fetches `limit*4` then dedupes in Python

If a single viewer reloads a profile 1000 times before the per-day
unique constraint takes effect (it's per-day, so within the same UTC
day this never happens after migration 0006 — but for the older
data already in the table), the `limit*4` could miss less-frequent
viewers. With the unique constraint live now this is moot; remove the
×4 over-fetch.

---

### L5 · Operator's `_abuse_target_context` swallows all exceptions

**File:** `app/routes/operator.py:_abuse_target_context`.

```python
try:
    messages = dms.list_messages(str(target_id), limit=20)
except Exception:
    messages = []
```

Already handled — `dms.list_messages` catches its own `PostgrestAPIError`.
Bare `except Exception` here is dead code that hides programmer errors.

---

### L6 · `network.list_directory_for_creator` does N+1 implicit lookup at the route

**File:** `app/routes/creator.py:network_directory` doesn't N+1, but
`/operator/jobs` and `/brand/jobs` and `/creator/jobs` all call
`profiles.get_creator_profile(pid)` in a dict comprehension over poster
ids. With 100 listings = 100 round-trips. Moves to a single
`profiles.get_creator_profiles_by_ids(ids)` once the list grows.

INFO for now (Phase 1 volumes), MED if listings ever scale to thousands.

---

### L7 · `/creator/notifications/{id}/read` re-renders /creator/notifications even on noop

If the notification doesn't belong to the user, `notifications.mark_read`
returns False, but the redirect still goes to `target` (or
`/creator/notifications`). Acceptable behavior, just note that the page
won't say "we couldn't find that notification."

---

### L8 · `app/templates/creator/dm_list.html` calls `profiles.get_creator_profile(pid)` for each thread peer in the route

Same N+1 as L6. Tiny volumes today.

---

### L9 · `app/services/notifications.create` allows duplicate notifications

If a brand sends the same outreach twice, two `collab_match` rows land.
The schema doesn't dedupe. UX-level only — operators can collapse later.

---

## INFO

### I1 · Every service uses `service_role` → RLS is enforcement-dead

The whole `migrations/0004_rls_policies.sql` codifies what *should* be
enforced if any caller used the anon client with a JWT. Today nothing
does. This is the structural call your partner already flagged. Not
worth re-flipping for Phase 1; just be aware that adding a new
`@router.get(...)` that talks to Supabase via the service client carries
zero RLS protection — the developer is fully responsible for the authz
check.

### I2 · 30-day session, no idle timeout

Standard for "magic link sites." If you want stricter, drop
`SESSION_MAX_AGE` and refresh the cookie on every authenticated request.

### I3 · CSP is `unsafe-inline` for styles

A few templates use inline `style=` attributes (`dm_thread`, etc.).
Tightening the CSP requires moving them to classes. Worth it before
public launch; not before invite-only beta.

### I4 · No automated horizontal-authz test pattern

The test suite is 248 cases of happy-path + role-guard + validation. It
doesn't exercise the "user A reads user B's data" pattern systematically.
The one exception is `test_calendar_detail_only_owner` in
`tests/test_bookings.py`. Add a parametrized fixture per service — about
40 lines of test code:

```python
@pytest.mark.parametrize("path", [
    "/creator/calendar/<other-id>",
    "/creator/jobs/<other-id>/edit",
    ...
])
def test_cross_user_404(client, path, world):
    _signed_in(client, role="creator", user_id="me")
    world.add_other_user_resource(path)
    r = client.get(path.replace("<other-id>", "<known-id>"))
    assert r.status_code in (403, 404)
```

I'll write this if you want — it's a `tests/test_horizontal_authz.py`
file that adds maybe 12-18 cases.

### I5 · No tests for the CSRF middleware on multipart bodies

`tests/test_csrf.py` covers urlencoded bodies. The multipart parser in
`_extract_token_from_body` is hand-rolled and untested. Phase 1 has no
multipart forms (file upload doesn't exist yet) so it's never
exercised. INFO until uploads ship.

### I6 · `Procfile` worker / beat lines commented out

Right call — Phase 2/3 not yet written. Document at the top of
`app/tasks/__init__.py` (already done by the audit pass).

### I7 · `notifications.create` body-length bound 2000 ✓ but `_BODY_MAX = 2000` is a module constant; copy to schema CHECK if you want consistency

Schema column is `text` (unbounded). Caps live in the service. If a
direct-SQL writer ever inserts via Supabase Studio, no enforcement.
Acceptable for now.

---

## What I'd ship before opening signups

In rough priority:

1. **H1** (URL scheme validation) — 15 lines, real risk if any external
   visitor lands on a brand profile.
2. **H3** (`/report` standing check) — 30 lines, closes the operator-
   queue weaponization.
3. **H2** (public-fields projection) — 40 lines, prevents accidental
   leak via future template additions.
4. **M1, M2, M3** (service-layer defense-in-depth) — 30 lines total.
5. **M4** (closed listings 404 for non-owner creators) — 3 lines.
6. **M6** (session-secret enforced in staging) — 1 line.
7. **I4** (cross-user test parametrize) — 50 lines, gives you a regression
   net for everything above.

Total: maybe 4 hours of careful work, including tests. Everything else
is post-launch polish.

If you want me to write all of the above in one PR, say the word and
I'll do it. If your partner wants to take any of these, this doc is
the checklist.
