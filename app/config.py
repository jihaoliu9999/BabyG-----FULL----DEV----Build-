"""Application settings loaded from environment variables.

All values are server-side. Never import this module from templates or pass any
field to the client. The Settings instance is a singleton via `get_settings()`.

Phase 1 declares only the fields it actually reads. Phase 2 (Anthropic,
Celery, Google, Twilio, Resend, PostHog, Sentry, Tavily, Instagram) keeps
its env vars in Railway/.env for future use; pydantic's `extra="ignore"`
accepts them silently. Add typed fields here when the corresponding code
lands and starts reading them.
"""

from functools import lru_cache
from typing import Literal

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

    # Phase 2 assistant
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    @property
    def is_production(self) -> bool:
        return self.env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
