"""Phase 3 placeholder — Celery tasks (workers + scheduled jobs).

Intentionally empty in Phase 1. The `worker:` and `beat:` lines in the
Procfile reference `app.tasks.celery_app`, which does not exist yet —
they're commented out for that reason. When Phase 3 lands:

  1. Create `celery_app.py` here, exporting a Celery instance bound to
     `settings.celery_broker_url`.
  2. Add task modules (one per concern: intel push, content reminders,
     receipt scrape, etc.).
  3. Uncomment the Procfile lines and provision the Redis/Upstash URL.

DEPLOY.md §10 has the longer version.
"""
