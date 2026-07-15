# Auth + magic-link deliverability runbook

This is the operational checklist for the auth half of Phase 1. Code-side
hardening already shipped (`_assert_app_url` boot guard, error-kind
classification on `/auth/callback`, the resend-link CTA, the `/auth/code`
6-digit fallback). What's left here is **infrastructure work** in three
dashboards — Resend, your DNS host, and Supabase — plus a five-provider
test loop you run at the end.

The success metric is not "Resend reports delivery". It's: a fresh test
address at each of Gmail / iCloud / Outlook / Yahoo / a business domain
can complete the round-trip — receive the email in the **primary inbox**,
click the magic link, land signed in on the correct dashboard.

You will not be able to guarantee primary-inbox placement; every webmail
provider applies its own reputation model. What you *can* control: the
authentication records, the sender consistency, and the template
cleanliness. Get all three right and the spam rate drops sharply.

---

## 0. Pick the sender subdomain and stick with it

Decide once: `auth.babyg.ai` or `mail.babyg.ai`. Use it for **every** auth
email forever. Switching later resets the reputation you've built.

Recommendation: `auth.babyg.ai` (clearer intent; keeps a separate
`mail.babyg.ai` available for marketing if that ever ships).

Update the canonical app domain too (`babyg.ai` vs `www.babyg.ai`).
Pick one, 301-redirect the other, and make sure `APP_URL` in Railway
matches **exactly** — scheme, subdomain, and trailing-slash all agree
with what Supabase Auth has on its allow-list.

---

## 1. Resend — verify the sender subdomain

In the Resend dashboard:

1. **Domains → Add Domain → `auth.babyg.ai`**.
2. Resend will give you four DNS records to publish (see §2):
   - One **TXT** for SPF
   - One **TXT** for DKIM (1024-bit)
   - One **TXT** for DMARC (optional from Resend, but ship one)
   - One **MX** + **TXT** for a custom return-path (so bounces come back
     to a subdomain you own, not `bounce.resend.com`)
3. After DNS propagates (5 min – 24 h), come back and click **Verify**.
   All four records must show ✅.
4. Disable **Open tracking** and **Click tracking** for the auth-email
   template. Link rewriting via Resend's tracking subdomain confuses
   some webmail clients into munging the magic-link query string, and
   the tracking pixels add a "promotional" signal that Gmail's
   importance classifier hates.

---

## 2. DNS records to publish

Replace `<resend-provided>` with whatever Resend gave you on the previous
step. Names that have no host part (just `@`) apply to the apex of
`auth.babyg.ai`.

| Type | Host | Value |
| --- | --- | --- |
| TXT | `auth` | `v=spf1 include:amazonses.com ~all` (or whatever Resend specifies) |
| TXT | `resend._domainkey.auth` | `<resend-provided DKIM public key>` |
| MX | `auth` | `10 feedback-smtp.<region>.amazonses.com` (Resend custom return-path) |
| TXT | `auth` | `<resend-provided custom return-path SPF>` |
| TXT | `_dmarc.auth` | `v=DMARC1; p=none; rua=mailto:dmarc@babyg.ai; pct=100` |

Notes:

- **Start DMARC at `p=none`.** Monitor the `rua=` reports for two weeks.
  Once you see only legitimate sources signing for `auth.babyg.ai`,
  escalate to `p=quarantine; pct=25`, watch a week, then `pct=100`,
  then `p=reject`. Never start at `reject` — one missed source and
  every magic link bounces.
- **Do not also publish records for the apex `babyg.ai`** from this
  subdomain's setup. The subdomain is the boundary; the apex stays
  managed separately.

---

## 3. Supabase Auth — point at Resend, match the redirect

In the Supabase dashboard for the project:

1. **Project Settings → Authentication → SMTP Settings**:
   - Enable custom SMTP.
   - Host: Resend's SMTP host (`smtp.resend.com`).
   - Port: `465` (TLS) or `587` (STARTTLS).
   - Username: `resend`.
   - Password: the Resend API key.
   - Sender email: `auth@auth.babyg.ai`.
   - Sender name: `babyg`.

2. **Project Settings → Authentication → URL Configuration**:
   - **Site URL**: `https://babyg.ai` (the canonical, exactly matching
     `APP_URL` in Railway).
   - **Redirect URLs (allow-list)**: add `https://babyg.ai/auth/callback`
     *and* every staging variant. Each redirect Supabase issues must
     have an exact prefix match here or the link 4xxs on click.

3. **Project Settings → Authentication → Email Templates**:
   - **Magic Link** template: rewrite to the minimal template in §4.

4. Restart any background workers that cache the Supabase config (none
   in babyg today, but worth noting if that ever changes).

---

## 4. Magic-link email template — minimal & low-spam

Replace the default Supabase template with the following. **No images.
No marketing footer. Plain copy, one CTA, consistent `From`.**

```html
<p>Hi there,</p>
<p>Tap the button below to finish signing in to babyg.</p>
<p>
  <a href="{{ .ConfirmationURL }}"
     style="display:inline-block;padding:12px 18px;background:#FF4D6D;
            color:#0a0a0a;text-decoration:none;border-radius:8px;
            font-family:Helvetica,Arial,sans-serif;font-weight:600">
    Sign in to babyg
  </a>
</p>
<p>Or paste this 6-digit code into the "got a code instead?" page:</p>
<p style="font-family:ui-monospace,monospace;font-size:22px;letter-spacing:4px">
  {{ .Token }}
</p>
<p>This link and code expire in 15 minutes.</p>
<p>If you didn't request this, ignore the email.</p>
<p>— babyg</p>
```

Why:

- Single CTA. Multiple links degrade reputation.
- The 6-digit code matches the `/auth/code` fallback that ships in code.
  If the magic link breaks (link-rewriting, copy/paste between devices),
  the user has a recovery path that doesn't require sending another
  email.
- No `unsubscribe` footer for transactional auth mail — required for
  marketing, harmful here. (CAN-SPAM exempts transactional mail.)
- No tracking pixel.

---

## 5. The five-provider test loop

Required addresses (use fresh, not previously-tested accounts to avoid
reputation carry-over):

- A Gmail address (`@gmail.com`).
- An iCloud address (`@icloud.com` or `@me.com`).
- An Outlook / Hotmail address (`@outlook.com` or `@hotmail.com`).
- A Yahoo address (`@yahoo.com`).
- A real business domain — e.g. an `@<yourcompany>.com` you administer.
  This is the hardest one: corporate spam filters are stricter than
  webmail. If this one passes, the rest almost always do.

For each address, run the full loop:

1. POST to `/auth/magic-link` with the address (role=creator).
2. Confirm the email arrives within 60 seconds.
3. Confirm it lands in the **primary inbox** — not Promotions, not
   Spam, not Updates.
4. Open the email **on a desktop browser**. Click the magic link.
   Verify it lands on `/creator` (or `/onboarding/creator` for a fresh
   account).
5. Open a separate session. POST `/auth/magic-link` for the same address.
   Open the email on a **mobile browser**. Repeat.
6. Open another session. POST `/auth/magic-link`. Wait 20 minutes (past
   the 15-minute expiry). Click the now-expired link. Confirm
   `/auth/callback` renders the expired-link recovery page with the
   "send me a new link" CTA pointed at `/auth/login?role=creator&hint=expired`.
7. Open another session. POST `/auth/magic-link`. On the "check your
   email" page, click **"got a code instead?"**. Paste the 6-digit code
   from the email into `/auth/code`. Confirm it signs you in.

Pass criteria: 5/5 providers complete step 4. 5/5 complete step 6
(expired path) and step 7 (code fallback). Spam-folder placement on any
provider counts as a fail; do not ship growth until it's resolved.

---

## 6. After-the-fact monitoring

- Publish DMARC aggregate reports to a mailbox you actually read.
  `dmarc@babyg.ai` works; pipe it through a free aggregator (Postmark
  DMARC, dmarcian.com) for the first month so you can spot any
  unauthorized sender attempting to sign for `auth.babyg.ai`.
- Sign up for **Google Postmaster Tools** and add `babyg.ai`. Watch the
  spam-rate, reputation, and DKIM-failure dashboards weekly.
- Sign up for **Microsoft SNDS** (for Outlook/Hotmail deliverability).
  Same idea.
- Set a `logger.info("magic-link send", extra={"provider": "gmail", ...})`
  if you ever want to slice send-success by recipient provider — careful
  not to log the address itself; hash it.

---

## 7. Common failure modes and what they look like

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| User clicks link → Supabase "redirect_uri error" page | `APP_URL` mismatches Supabase's Redirect URLs allow-list | Make them byte-identical, including trailing slash and protocol |
| Email lands in Promotions / Updates on Gmail | Open/click tracking enabled, or template has marketing-style imagery | Disable tracking; strip images; one CTA |
| Email lands in Spam on Outlook | Missing SPF or DKIM (Outlook is the strictest of the big four on auth) | Re-verify Resend, ensure both records propagate, retest |
| Email arrives but link is mangled (extra query params, double-encoded) | Resend click-tracking rewriting the URL | Disable click-tracking for the auth template |
| Link clicks land on `/auth/error?` with a generic message | `_assert_app_url` should already have caught this at boot — verify your env, then check whether Supabase's Site URL was changed without updating the Railway env var |
| First-time operator gets a friendly-looking link but `/auth/callback` errors with role-mismatch | Intended behavior. Operator is invite-only — they need a pre-existing `public.users` row. Provision them via an admin, then they can sign in. |

---

## 8. Rollback plan

Each step is reversible:

- DNS records: have a 5-minute TTL while bedding in; you can revert at
  any time.
- Supabase SMTP: flip back to "Use default" (Supabase's own SMTP); auth
  works, sender just becomes `noreply@mail.app.supabase.io` again.
- App code: `_assert_app_url`, `/auth/code`, and the error-kind fork
  are all behind no feature flag because they're pure additions — but
  reverting the branch that introduced them is the rollback path. The
  legacy "send me a magic link" form still works without any of them.
- Tracking: `dmarc=none` is safe; never bump to `quarantine`/`reject`
  before the aggregate reports show all expected sources.

---

## 9. Phase-1 exit criterion

All of:

- [ ] `_assert_app_url` is in `app/main.py` and crashes the boot when
      `APP_URL` is wrong in non-dev. *(shipped: commit `549bf3e`)*
- [ ] Resend has `auth.babyg.ai` showing all four records ✅.
- [ ] Supabase Auth Site URL matches `APP_URL` exactly.
- [ ] Supabase redirect-allow-list contains the prod callback.
- [ ] DMARC report destination is monitored.
- [ ] Five-provider test loop passes 5/5 on the magic-link round-trip,
      5/5 on the expired-link recovery, 5/5 on the code-fallback path.
- [ ] No spam-folder placements on any tested provider.

Only after every box is checked do we move on to Phase 2 (brand role
foundation).
