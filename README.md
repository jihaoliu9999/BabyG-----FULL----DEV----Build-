# babyg

Hybrid AI social media management platform for lifestyle creators and brands in Miami.

## Stack

- **Backend:** FastAPI (Python 3.12), Celery worker, Celery Beat scheduler — three Railway processes sharing one codebase.
- **Database / Auth / Realtime:** Supabase (Postgres + magic-link auth + Realtime).
- **AI:** Anthropic Claude (`claude-sonnet-4-6`) — Central Bot for Hot Drop broadcasts, babyg agent for conversational tool use.
- **Frontend:** Server-rendered Jinja2 templates progressively enhanced into a PWA. Dark mode only.
- **Calendar:** Google Calendar API (Gmail + Calendar share one OAuth grant).

The AI assistant is named **babyg** everywhere unless a creator renames it.

## Local development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env   # fill in keys
python run.py          # http://127.0.0.1:8000/healthz
```

## Tests

```bash
pytest
```

## Project layout

```
app/
  main.py            FastAPI factory + /healthz
  config.py          pydantic-settings (env loader)
  core/              session signing, supabase/redis clients, rate limit
  routes/            landing, auth, onboarding, creator/, brand/, operator/
  services/          prompts.py (single source), claude, context, moderation, etc.
  agent/             agentic loop, tool registry, approval gating
    tools/           one file per Claude tool
  integrations/      external API clients (Anthropic, Tavily, Google, Resy, ...)
  tasks/             Celery tasks — names match Section 10.4 of the deck
  templates/  static/
migrations/          Supabase SQL migrations + RLS
tests/
```

## Build phases

See `babyg_product- stack v1` (the product manual) Section 16 for the full phased plan.
Current phase: **Phase 1 — Foundation**.

## Rules

- Python only.
- Every prompt lives in `app/services/prompts.py`. No prompts elsewhere.
- API keys never reach templates or client JS.
- Three user types: Creator, Brand, Operator. Each has a separate interface.
