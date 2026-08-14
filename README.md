# babyg

Where creators and brands meet, match, and manage — with an ai manager on
every account.

## What's shipped

- **Backend:** FastAPI (Python 3.12), single web process.
- **Database / Auth:** Supabase (Postgres + magic-link OTP).
- **Frontend:** Server-rendered Jinja2 templates. Dark mode only.
- **Surfaces:** marketing landing, magic-link auth, role-gated consoles for
  **Creator**, **Brand**, and **Operator** — creator DMs, network /
  connections, job listings, calendar, content receipts, performance
  insights, intel feed; brand discovery, DMs, and console; operator intel
  publishing, abuse reports, member roster, audit log.
- **AI:** in-app AI assistant / agent powered by Anthropic Claude
  (`app/agent/`, `app/services/bot.py`, `app/services/prompts.py`).
- **Integrations:** Google (Gmail + Calendar, single OAuth grant with
  active revoke on disconnect), Instagram/Meta (read-only insights),
  Tavily (web search for the AI agent), Anthropic (Claude).
- **Security:** signed session cookie, CSRF middleware, security headers,
  rate-limited magic-link, RLS policies on every Supabase table, active
  token revocation at disconnect for Google.

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
pytest                           # 850+ tests
ruff check .                     # lint
```

CI runs both on every push and pull request (`.github/workflows/ci.yml`).

## Project layout

```
app/
  main.py            FastAPI factory + middleware (CSRF, GZip, security headers)
  config.py          pydantic-settings env loader
  core/              session signing, supabase clients, CSRF, rate-limit, redirects
  routes/            marketing, auth, onboarding, creator, brand, operator, abuse
  services/          DB access for each domain (dms, network, intel, jobs, bot, prompts, ...)
  agent/             AI agent loop + tool registry
    tools/           one file per Claude tool
  integrations/      external API clients (anthropic, google_calendar, google_gmail, instagram_meta, tavily)
  templates/  static/
migrations/          Supabase SQL migrations + RLS
scripts/             ops helpers (generate_session_secret.py)
tests/               pytest suite
```

## Deployment

See `DEPLOY.md` for the Railway + Supabase playbook.

## Rules

- Python only.
- Every prompt lives in `app/services/prompts.py`. No prompts elsewhere.
- API keys never reach templates or client JS.
- User roles: Creator, Brand, Operator.
