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

from pydantic import AliasChoices, Field
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
    public_app_url: str = Field(
        default="",
        validation_alias=AliasChoices("PUBLIC_APP_URL", "SITE_URL"),
    )
    session_secret: str = "dev-only-not-secure-replace-me"
    # When true, a missing migration on Supabase crashes the boot
    # instead of just logging a WARN. Off by default so flipping the
    # toggle is an explicit operational decision per env.
    strict_migration_check: bool = False

    # Supabase
    supabase_url: str = ""
    supabase_anon_key: str = ""
    supabase_service_role_key: str = ""

    # Phase 2 assistant
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-4-20250514"

    # Google OAuth / Calendar
    google_client_id: str = ""
    google_client_secret: str = ""
    google_redirect_uri: str = ""
    google_oauth_scopes: str = (
        "https://www.googleapis.com/auth/calendar.events"
    )

    # Tavily — web search tool the babyg agent loop uses when current
    # public info (events, venue openings, brand news) is needed.
    # Empty key = tool is unavailable; the bot routes around it.
    tavily_api_key: str = ""

    # Instagram / Meta — read-only stats integration. Same shape as
    # the Google fields: empty values = integration unavailable, the
    # bot tool returns {"available": false} and the integrations grid
    # surfaces "not configured". Redirect URI defaults to
    # `${app_url}/creator/instagram/callback` when empty (computed at
    # use site in instagram_meta.redirect_uri).
    #
    # AliasChoices accept the Meta-dashboard-style names (META_APP_ID
    # / META_APP_SECRET / FACEBOOK_APP_ID / FACEBOOK_APP_SECRET) in
    # addition to the canonical INSTAGRAM_* names, since operators
    # routinely copy these straight from the Meta App dashboard where
    # the field is just called "App ID" / "App Secret" without an
    # Instagram-specific prefix. First non-empty match wins.
    instagram_app_id: str = Field(
        default="",
        validation_alias=AliasChoices(
            "INSTAGRAM_APP_ID",
            "META_APP_ID",
            "FACEBOOK_APP_ID",
        ),
    )
    instagram_app_secret: str = Field(
        default="",
        validation_alias=AliasChoices(
            "INSTAGRAM_APP_SECRET",
            "META_APP_SECRET",
            "FACEBOOK_APP_SECRET",
        ),
    )
    instagram_redirect_uri: str = Field(
        default="",
        validation_alias=AliasChoices(
            "INSTAGRAM_REDIRECT_URI",
            "META_REDIRECT_URI",
        ),
    )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def magic_link_callback_url(self) -> str:
        """Public callback used only by Supabase passwordless email auth."""
        public_origin = self.public_app_url or self.app_url
        return f"{public_origin.rstrip('/')}/auth/callback"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
