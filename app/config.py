"""Application settings loaded from environment variables.

All values are server-side. Never import this module from templates or pass any
field to the client. The Settings instance is a singleton via `get_settings()`.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    env: Literal["dev", "staging", "production"] = "dev"
    app_url: str = "http://localhost:8000"
    session_secret: str = "dev-only-not-secure-replace-me"

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # Anthropic
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-6"

    # Redis / Celery
    redis_url: str = ""
    celery_broker_url: str = ""

    # Tavily
    tavily_api_key: str = ""

    # Google (Gmail + Calendar)
    google_client_id: str = ""
    google_client_secret: str = ""
    google_oauth_scopes: str = (
        "https://www.googleapis.com/auth/gmail.readonly "
        "https://www.googleapis.com/auth/gmail.send "
        "https://www.googleapis.com/auth/calendar.events"
    )

    # Instagram
    instagram_app_id: str = ""
    instagram_app_secret: str = ""

    # Resy
    resy_api_key: str = ""
    resy_auth_token: str = ""

    # OpenTable
    opentable_client_id: str = ""
    opentable_client_secret: str = ""

    # Duffel
    duffel_api_key: str = ""

    # Twilio
    twilio_account_sid: str = ""
    twilio_auth_token: str = ""
    twilio_phone_number: str = ""

    # Resend
    resend_api_key: str = ""

    # PostHog
    posthog_api_key: str = ""
    posthog_public_key: str = ""

    # Sentry
    sentry_dsn: str = ""

    # Feature flags
    scope_precheck_enabled: bool = True
    tool_use_enabled: bool = True

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
