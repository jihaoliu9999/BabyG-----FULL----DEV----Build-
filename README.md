# babyg

Hybrid AI social media management platform for lifestyle creators and brands in Miami.

## Phase 1 (current)

What's actually shipped:

- **Backend:** FastAPI (Python 3.12), single web process.
- **Database / Auth:** Supabase (Postgres + magic-link OTP).
- **Frontend:** Server-rendered Jinja2 templates. Dark mode only.
- **Surfaces:** marketing landing, magic-link auth, role-gated consoles
  for **Creator**, **Brand**, **Operator** — DMs, network/connections,
  job listings, calendar, content receipts, weekly performance, intel
  feed, brand verification queue, abuse reports, member roster, audit log.
- **Security:** signed session cookie, CSRF middleware, security headers,
  rate-limited magic-link, RLS policies on every Supabase table.

## Phase 2 (planned, not built)

These are stubs: the directories and env vars exist, but nothing in `app/`
imports them yet. They land in their own steps.

- Anthropic Claude (Central Bot for Hot Drops, babyg agent for tool use)
- Celery worker + Beat (proactive nudges, scheduled posts)
- Google Calendar sync (Gmail + Calendar share one OAuth grant)
- External integrations (Tavily, Instagram, OpenTable, Duffel, Twilio, Resend)
- PWA assets, realtime DMs, observability (PostHog + Sentry)

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env             # fill in SUPABASE_* and SESSION_SECRET
python scripts/generate_session_secret.py >> .env   # appends a fresh secret
python run.py                    # http://127.0.0.1:8000/healthz
```

## Tests

```bash
pytest                           # 197+ tests
ruff check .                     # lint
```

CI runs both on every push and pull request (`.github/workflows/ci.yml`).

## Project layout

```
app/
  main.py            FastAPI factory + middleware (CSRF, GZip, security headers)
  config.py          pydantic-settings env loader
  core/              session signing, supabase clients, CSRF, rate-limit, redirects
  routes/            marketing, auth, onboarding, creator/, brand/, operator/, abuse
  services/          DB access for each domain (dms, network, intel, jobs, ...)
  agent/             — Phase 2 — agent loop, tool registry
    tools/           — Phase 2 — one file per Claude tool
  integrations/      — Phase 2 — external API clients
  tasks/             — Phase 2 — Celery tasks
  templates/  static/
migrations/          Supabase SQL migrations + RLS (0001-0006)
scripts/             ops helpers (generate_session_secret.py)
tests/               pytest suite
```

## Deployment

See `DEPLOY.md` for the end-to-end Railway + Supabase + Resend playbook.

## Rules

- Python only.
- Every prompt lives in `app/services/prompts.py` (Phase 2). No prompts elsewhere.
- API keys never reach templates or client JS.
- Three user types: Creator, Brand, Operator. Each has a separate interface.
