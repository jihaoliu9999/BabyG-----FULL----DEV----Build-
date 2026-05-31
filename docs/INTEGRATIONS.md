# Integrations roadmap

Forward-looking technical notes for integrations that babyg does NOT
implement today. These are scoped for future phases. Adding any of
them touches schema, scopes, app reviews, or compliance work that is
intentionally out of band for current bug-fix passes.

The chatbot prompt's "stats reality check" section is the contract
that protects users until these ship — Claude is told to never claim
live platform stats exist while these remain unconnected.

---

## 1. Meta / Instagram auto-sync (future)

**Scope:** pull Instagram post insights (likes, comments, reach,
impressions, saves) into `read_my_performance` and `read_my_receipts`
automatically, so the bot can answer "how did my last post do?" from
real data instead of the manual-log fallback.

**What's required:**

- **Meta Developer App** registered under a Facebook Business Manager.
- Creator's **Instagram account must be a Business or Creator account
  linked to a Facebook Page** (personal accounts cannot use the
  Graph API).
- **Instagram Graph API** access. Phase 1 scopes:
  - `instagram_basic` — read profile + media
  - `instagram_manage_insights` — read post insights
  - `pages_show_list`, `pages_read_engagement` — required to traverse
    the FB Page → IG Business Account edge.
- **Meta App Review** to use the production-tier scopes outside dev
  mode. Plan ~4–6 weeks turnaround for first submission.
- **OAuth 2.0** flow with `state` + PKCE. Long-lived (60-day) tokens
  stored encrypted in `creator_oauth_connections` (existing table,
  already used for Google Calendar — same pattern).
- **Token refresh** logic: re-exchange before the 60-day window
  closes; surface a "reconnect Instagram" CTA when expired.
- **Insight mapping:**
  - `media_id` → `like_count`, `comments_count`, `reach`, `impressions`, `saves`
  - Story / reel / carousel each have different available metrics.
- **Update path:**
  - Cron pull (e.g. nightly) on `creator_oauth_connections` rows with
    `provider = 'meta'`, OR
  - Meta Webhooks subscription for real-time `media` events. Webhooks
    need a public callback URL and signature verification.
- **Rate limits + error handling:** the Graph API uses per-user-token
  buckets; back off on `code: 4` / `code: 17` errors.

**Privacy / policy:**

- Update privacy policy + Terms of Service to disclose Meta data use.
- Explicit opt-in screen before kicking off OAuth.
- Disconnect flow must revoke the token and clear cached insights.

---

## 2. TikTok auto-sync (future)

**Scope:** pull TikTok video insights into `read_my_performance` /
`read_my_receipts`.

**What's required:**

- **TikTok for Developers** app registration.
- **TikTok Login Kit** for OAuth + identity.
- **Display API** (lighter) or **Marketing API** (richer insights,
  approval-gated) depending on the metrics we need.
- Scopes:
  - `user.info.basic` — handle, avatar, display name
  - `video.list` — list creator's videos
  - `video.insights` — view, like, comment, share counts (Display API)
- **TikTok App Review** for production scopes. Plan ~2–4 weeks.
- **OAuth 2.0** flow + refresh tokens. Store encrypted in
  `creator_oauth_connections` with `provider = 'tiktok'`.
- **Insight mapping:**
  - `video_id` → `view_count`, `like_count`, `comment_count`,
    `share_count`, `average_watch_time`
- **Rate limits:** per-app and per-user buckets; back off on
  `error_code: 12100` (rate limit) and `error_code: 10010` (token
  expired).

**Privacy / policy:** same disclosure + opt-in + disconnect
requirements as Meta.

---

## 3. SMS chatbot memory (future)

**Scope:** let creators message babyg over SMS and have the
conversation thread into the same `bot_messages` history as the web
chat.

**What's required:**

- **SMS provider:** Twilio is the default; Telnyx / MessageBird are
  cost-competitive alternatives. Provision a US local number (or
  toll-free for higher per-day throughput).
- **Phone verification:** verify-code-over-SMS flow; store the hash
  of the verified phone number against `users.id` in a new
  `creator_phones` table (or a column on `creator_profiles`).
- **Inbound webhook handler:** Twilio POSTs incoming SMS to a public
  endpoint. Verify the Twilio signature on every request.
- **Identity link:** phone-number → user_id lookup. Reject messages
  from unverified numbers.
- **Thread storage:** write SMS exchanges into `bot_messages` with a
  new `source = 'sms'` column (one-line migration when shipped). The
  agent loop can read these exactly like web messages — the only
  difference is the egress (Twilio API → SMS) instead of HTML render.
- **TCPA compliance:**
  - Explicit opt-in before the first message.
  - Honor `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END`, `QUIT`
    keywords. Persist opt-out in a separate "do-not-text" table that
    is checked before every outbound message.
  - Honor `HELP` keyword with a canned response.
  - Disclose message frequency + standard messaging rates apply.
- **Rate limiting:** per-phone-number outbound cap (e.g. 30/day) +
  global daily cap to prevent runaway cost.
- **Spam reporting:** monitor Twilio carrier feedback; auto-pause
  numbers with high spam scores.
- **Retention:** allow creator to delete SMS history; align with
  current `bot_messages` retention policy.

**Cost note:** Twilio charges per inbound + outbound SMS. Budget +
alerts must be wired before launch.

---

## Why this file exists

These integrations come up regularly during product discussion. This
doc keeps the *what's required* and *why it's not done yet* in one
place so we don't re-derive it each time. When any of them moves into
an actual implementation phase, a separate design doc + migration
plan should land first.
