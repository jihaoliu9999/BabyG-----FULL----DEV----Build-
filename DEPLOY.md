# babyg deployment playbook

End-to-end, exact-clicks runbook to take Phase 1 from a local dev branch
to a working invite-only site at your own domain. Everything below is
ops; no more code is needed for Tier 1.

**Target time end-to-end:** ~45 minutes once the prerequisites are ready.

> Conventions: text in `code` is exact strings to paste. Keep this open
> in one tab and the relevant dashboard in another.

---

## 0 · Prerequisites

You need accounts on:

- [ ] **Supabase** — project already exists (`ziqiokrjwzukzhaiyhpv`, `babyg`)
- [ ] **Railway** — sign up at railway.app, link your GitHub
- [ ] **Resend** — sign up at resend.com (10 minutes; needs a domain to verify)
- [ ] **Your domain registrar** (Namecheap, Cloudflare, Squarespace, etc.) for DNS records

Local repo has been pushed to:
- `https://github.com/jihaoliu9999/babyg-----full----dev----build-`
- branch: `claude/babyg-platform-planning-Xxvsn`

Phase 1 checked-in artifacts already include `Procfile`, `railway.json`,
`requirements.txt`, `.env.example` — Railway picks these up automatically.

---

## 1 · Rotate the leaked Supabase service-role key (1 minute)

You pasted the original `service_role` key in chat earlier, so it sits in
this transcript. Rotate before deploying anywhere.

1. Go to https://supabase.com/dashboard/project/ziqiokrjwzukzhaiyhpv/settings/api
2. Find the **service_role secret** card → **Reset service role secret**
3. **Copy the new value into a password manager** (1Password, Bitwarden, etc.).
   You'll paste it into Railway in step 4. Don't paste it back into chat.
4. Note the **anon public key** from the same page — you also need that
   for Railway. Copy it too.

While you're here, also note:

- **Project URL**: `https://ziqiokrjwzukzhaiyhpv.supabase.co`

---

## 2 · Configure Supabase Auth for the production origin (5 minutes)

Magic-link emails won't work until Supabase knows where to redirect after
the user clicks the link in their email.

1. Go to **Authentication → URL Configuration**
   `https://supabase.com/dashboard/project/ziqiokrjwzukzhaiyhpv/auth/url-configuration`

2. **Site URL**: set to your eventual prod URL.
   - If you haven't set up the custom domain yet, use the Railway-provided
     URL temporarily (you'll get one in step 4 — `https://babyg-xxxx.up.railway.app`).
   - You'll come back here later to swap to your custom domain.

3. **Redirect URLs (allowlist)** — add these exact entries, one per line:
   ```
   https://your-prod-domain.com/auth/callback
   https://babyg-xxxx.up.railway.app/auth/callback
   http://localhost:8000/auth/callback
   ```
   The localhost entry is for local dev. The Railway one is the fallback
   if your custom domain isn't live yet. The custom domain entry is for
   later — once your domain works, you can drop the railway.app one.

4. Click **Save**.

---

## 3 · Wire Resend as Supabase Auth's SMTP provider (10 minutes)

Default Supabase Auth email is rate-limited to 4 emails/hour with babyg
unbranded "from" — fine for dev, blocking for real signups. Resend handles
SMTP, branded from-address, and gives you 100 free emails/day forever.

### 3a. Set up Resend

1. Sign up at https://resend.com
2. **Domains → Add Domain** → enter the domain you'll use for the from-
   address (e.g. `mail.your-prod-domain.com` if your main domain is
   `your-prod-domain.com`; using a subdomain is a good practice).
3. Resend shows you 4 DNS records (MX, 2× TXT, CNAME). Add them in your
   registrar's DNS tab. Verification typically takes 5-30 minutes.
4. Once verified (status: green), go to **API Keys → Create API Key**.
   Name it `supabase-smtp`. **Sending Access** = Full access. Copy the
   `re_xxxxx` value into your password manager.

### 3b. Tell Supabase to use Resend

1. Go to **Authentication → Emails → SMTP Settings**
   `https://supabase.com/dashboard/project/ziqiokrjwzukzhaiyhpv/auth/emails`

2. Enable **Custom SMTP**. Fields:
   ```
   Sender email:    babyg@mail.your-prod-domain.com
   Sender name:     babyg
   Host:            smtp.resend.com
   Port:            465
   Username:        resend
   Password:        re_xxxxx  (the API key from 3a)
   Minimum interval: 60 seconds
   ```

3. **Save**.

4. Test: from the same page, **Send test email** to your own address.
   It should arrive within seconds, from `babyg@mail.your-domain.com`.

### 3c. (Optional but recommended) Customize the magic-link email

Same page → **Email Templates → Magic Link**. Replace the default body
with something on-brand. The subject line is what your users will see in
their inbox notifications, so make it short and obvious — e.g.
`Your sign-in link for babyg`.

---

## 4 · Deploy to Railway (15 minutes)

### 4a. Create the project

1. Sign in to https://railway.app
2. **New Project → Deploy from GitHub repo**
3. Pick `jihaoliu9999/babyg-----full----dev----build-`
4. Railway detects `Procfile` + `requirements.txt` and starts a build.
   First build takes ~3 minutes.

### 4b. Set environment variables

While the first build runs, click into the new service → **Variables tab
→ Raw Editor** and paste the block below. Replace every `<...>` with your
real value.

```
ENV=production
APP_URL=https://babyg-xxxx.up.railway.app

SESSION_SECRET=<run: python -c "import secrets; print(secrets.token_urlsafe(48))">

SUPABASE_URL=https://ziqiokrjwzukzhaiyhpv.supabase.co
SUPABASE_ANON_KEY=<the anon key you noted in step 1>
SUPABASE_SERVICE_ROLE_KEY=<the rotated service_role key from step 1>

# Phase 2 (bot) — leave blank for now; the app boots fine without them
ANTHROPIC_API_KEY=
ANTHROPIC_MODEL=claude-sonnet-4-6

# Optional now, required later (worker step):
REDIS_URL=
CELERY_BROKER_URL=

# Optional Phase 2 integrations — leave blank for now:
TAVILY_API_KEY=
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=
INSTAGRAM_APP_ID=
INSTAGRAM_APP_SECRET=
OPENTABLE_CLIENT_ID=
OPENTABLE_CLIENT_SECRET=
DUFFEL_API_KEY=
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=

# Email transport (used directly by app for non-magic-link transactional sends later)
RESEND_API_KEY=<the Resend API key from step 3a>

# Observability — set later
POSTHOG_API_KEY=
POSTHOG_PUBLIC_KEY=
SENTRY_DSN=

# Feature flags
SCOPE_PRECHECK_ENABLED=true
TOOL_USE_ENABLED=true
```

Railway will redeploy automatically when you save variables.

### 4c. Get your Railway URL

1. **Settings tab → Networking → Generate Domain**
2. Copy the URL (`https://babyg-xxxx.up.railway.app`)
3. Paste it into the `APP_URL` variable above (replace the placeholder
   you set initially). Save.
4. Go back to **Supabase Authentication → URL Configuration** (step 2)
   and confirm this exact URL is in both **Site URL** and the
   **Redirect URLs** allowlist.

### 4d. Verify the deployment

```bash
curl https://babyg-xxxx.up.railway.app/healthz
# → {"status":"ok","env":"production"}
```

Open `https://babyg-xxxx.up.railway.app/` in a browser. You should see
the dark landing page with **Get started** and **I have an account** CTAs.

---

## 5 · Provision the first operator (5 minutes)

Operators are invite-only — there's no signup form. You need to manually
flip yourself to operator the first time.

### 5a. Trigger your magic link as a "creator"

1. Go to your deployed `/get-started` → **Admin** card → enter your email →
   submit. Yes, ignore the warning that says "Operator access is
   invite-only" — that gate works at the **callback** layer, not the
   request layer. Your magic-link email *will* arrive.

   (Wait — actually re-read the operator gate logic. The `/auth/magic-link`
   POST silently skips the OTP send for operator emails that don't already
   exist as operators. So the magic link won't arrive if you pick Admin.
   Pick **Creator** instead — the auth.users row is what we care about.)

2. **Pick Creator on `/get-started`** → enter your email → submit.

3. Open the email, click the link. You'll land on `/onboarding/creator`.
   You don't need to fill out the form yet — what you need is the
   `auth.users` row that was just created.

### 5b. Promote yourself to operator via Supabase SQL

1. Open **Supabase Studio → SQL Editor**:
   `https://supabase.com/dashboard/project/ziqiokrjwzukzhaiyhpv/sql/new`

2. Paste and run:

   ```sql
   -- 1. Find your auth.users row by email
   select id, email from auth.users where email = 'your@email.com';
   ```

   Copy the `id` (a UUID).

3. Promote in `public.users`:

   ```sql
   update public.users
   set role = 'operator'
   where email = 'your@email.com';

   -- Verify
   select id, email, role from public.users where email = 'your@email.com';
   ```

4. **Sign out** of the running site (the role is baked into your session
   cookie — you need a fresh sign-in for the operator role to take effect).

5. **Sign back in** via `/get-started → Admin → your email → click the
   email link**. This time the operator gate sees your `public.users`
   row and lets you through. You should land on `/operator`.

After this, you can promote additional operators directly from SQL or
build a UI for it later.

### 5c. Delete the abandoned creator-onboarding row (optional cleanup)

When you signed up as a "creator" in 5a, the auth callback inserted both
a `users` row (now flipped to operator) and an empty `creator_profiles`
row. The `creator_profiles` row is harmless — it never gets onboarded
and operators don't query it — but if you want it gone:

```sql
delete from public.creator_profiles where user_id = 'your-uuid-here';
```

---

## 6 · Custom domain (5 minutes + DNS time)

If you want `babyg.com` instead of `babyg-xxxx.up.railway.app`:

1. **Railway → Settings → Networking → Custom Domain → Add**
2. Enter your domain (e.g. `app.babyg.com` or `babyg.com`)
3. Railway shows DNS records — add them in your registrar
4. Wait for verification (5-60 minutes typically)
5. **Update `APP_URL` env var** in Railway to the new domain
6. **Update Supabase Site URL + Redirect URLs** (step 2) to include the
   new domain. Keep the railway.app URL in the allowlist as a fallback
   while you verify.

---

## 7 · Smoke-test the full creator + brand flow (10 minutes)

Before inviting anyone, run through both happy paths yourself in two
different browsers (so you're signed in twice).

### Browser A — creator flow

1. `/get-started` → Creator → email → click magic link
2. Complete onboarding (fill out everything; pick a few niches and a
   content format; tier = pro)
3. Land on `/creator` — should be empty (no intel yet)
4. Visit `/creator/network` — empty grid (no other creators yet)
5. Visit `/creator/calendar`, `/creator/receipts`, `/creator/performance`,
   `/creator/views`, `/creator/jobs` — all should render with empty states

### Browser B — operator flow

1. Sign in as operator (your promoted account from step 5)
2. Go to `/operator` — should show 0 pending across the board
3. **Publish your first intel post**: `/operator/intel/new` → fill out
   title, body, category=venue, valid_until=7 days out, target tiers =
   basic+pro+vip, target niches = empty (= all niches), status =
   **active** → save
4. Refresh Browser A on `/creator` — you should see the intel card.
   Click a feedback button to confirm it sticks.

### Browser C — brand flow

1. New incognito window: `/get-started` → Brand → different email →
   magic link
2. Complete brand onboarding
3. Land on `/brand` — should show the **verification pending** pane
4. Switch to Browser B (operator) → `/operator/brands` → review the new
   brand → **Verify**
5. Refresh Browser C on `/brand` → should now show the discovery grid
   with Browser A's creator
6. Click into the creator → **Send outreach** with a 20+ char pitch
7. Refresh Browser A → notification strip shows the outreach;
   `/creator/dm` shows the thread
8. Reply from Browser A; verify Browser C sees the reply on `/brand/dm`

If all of that works, you're shippable for an invite-only beta.

---

## 8 · What's NOT live (so you know what to tell early users)

When you invite the first 5-10 people, set expectations:

- **No AI cofounder yet.** The chat surface and Anthropic integration
  arrive in Phase 2. Today the platform is a manual "directory + intel
  feed + DM + verification" — useful, but not the differentiator.
- **No Google Calendar sync yet.** Bookings live in babyg only;
  two-way Google sync ships with the bot's tool use in Phase 2.
- **No scheduled / automated jobs.** Daily intel push, posting reminders,
  weekly digest — none of these run on a timer yet (we have the schema
  for `content_reminders` but no Celery worker). Manual operator
  publishing only.
- **No automated heuristic moderation.** All abuse is user-reported and
  operator-reviewed. Auto-flagging arrives with the agent layer.
- **No mobile app.** Mobile-web works (the CSS is mobile-first); native
  iOS/Android is post-Phase-3.

---

## 9 · Day-2 ops cheatsheet

### Inviting another operator

```sql
-- Step 1: tell them to sign up as a creator and click the link
-- Step 2: promote them
update public.users set role = 'operator' where email = 'them@example.com';
-- Step 3: tell them to sign out and sign back in
```

### Reading logs

Railway → service → **Deployments tab → View Logs**. Tail the live deploy.

### Rotating the SESSION_SECRET

If `SESSION_SECRET` ever leaks, rotate it in Railway env vars. Effect:
**every signed-in user is forced to re-sign-in** (their cookies become
unverifiable). This is the right behavior on rotation.

### Resetting a user

```sql
-- Wipe a specific user, cascading to all their content
delete from auth.users where email = 'them@example.com';
-- The public.users row + profile + DMs / connections / jobs etc.
-- cascade-delete via the foreign keys.
```

### Pausing the site

Railway → service → **Settings → Pause Service**. The site goes 503;
the database stays up.

---

## 10 · What to build next

In rough priority for a real launch:

1. **Phase 2: the bot.** Anthropic SDK + Claude Sonnet 4.6 + tool use
   (intel lookup, calendar, brief drafting, scope precheck). This is
   the actual product.
2. **Phase 3: Celery workers.** Daily intel push (5am Miami time),
   scheduled-intel auto-publish, posting reminders, weekly digest,
   flagged-message scan. Needs Redis (Upstash on Railway is easy).
3. **Phase 4: analytics + dashboards.** PostHog for product analytics,
   Sentry for error tracking, an operator analytics dashboard for
   bot_analytics + performance_logs aggregates.
4. **Polish:** real landing page, pricing page (when tiers go live),
   marketing site, mobile native if it ever matters.

When you're ready, say the word and we'll start Phase 2.
