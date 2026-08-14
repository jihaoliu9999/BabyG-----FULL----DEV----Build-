# Integrations roadmap

Forward-looking technical notes for the Instagram / Meta auto-sync path
that expands beyond what's shipped today. The current Instagram
integration (`app/integrations/instagram_meta.py`) is read-only insights
via Instagram Basic Display; this doc scopes the next tier.

The AI assistant's "stats reality check" prompt is the contract that
protects users until fuller integration ships — Claude is told to never
claim live platform stats exist while they remain unconnected.

---

## Instagram Graph API — post-level insights (future)

**Scope:** pull Instagram post insights (likes, comments, reach,
impressions, saves) into `read_my_performance` and `read_my_receipts`
automatically, so the AI can answer "how did my last post do?" from
real data instead of the manual-log fallback.

**What's required:**

- **Meta Developer App** registered under a Facebook Business Manager.
- Creator's **Instagram account must be a Business or Creator account
  linked to a Facebook Page** (personal accounts cannot use the
  Graph API).
- **Instagram Graph API** scopes:
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

- Update privacy policy + terms to disclose the deeper Meta data use.
- Explicit opt-in screen before kicking off OAuth.
- Disconnect flow must revoke the token and clear cached insights
  (existing Google-revoke pattern applies — see
  `app/services/oauth_connections.py:disconnect_google`).
