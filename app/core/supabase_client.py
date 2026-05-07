"""Supabase client factories.

Two clients with very different blast radius:

  * `get_anon_client()` — uses the anon key. Subject to RLS. Used for auth
    flows (sign_in_with_otp, verify_otp). One per request is fine; the
    underlying httpx client pools connections.

  * `get_service_client()` — uses the service-role key. **Bypasses RLS.**
    Use only from trusted server paths (auth callback, Celery jobs, admin
    operations). Never construct this from anything that handles user input
    without first authorizing the caller.

Both are cached by `lru_cache` so we don't reinstantiate per-request.
"""

from __future__ import annotations

from functools import lru_cache

from supabase import Client, create_client

from app.config import get_settings


@lru_cache(maxsize=1)
def get_anon_client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_anon_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_ANON_KEY must be set")
    return create_client(settings.supabase_url, settings.supabase_anon_key)


@lru_cache(maxsize=1)
def get_service_client() -> Client:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError(
            "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set for service client"
        )
    return create_client(settings.supabase_url, settings.supabase_service_role_key)
