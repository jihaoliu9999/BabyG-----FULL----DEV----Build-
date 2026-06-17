"""Settings env-var alias tests.

The Instagram fields accept multiple env var names because operators
routinely copy "App ID" / "App Secret" straight out of the Meta App
dashboard and end up naming them META_APP_ID / FACEBOOK_APP_ID.

We pin the alias precedence + still-reads-canonical behavior so the
diagnostic at /creator/_debug/integrations stays trustworthy.
"""

from __future__ import annotations

import pytest

from app.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _reset_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_instagram_canonical_env_names_are_read(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "ig-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "ig-secret")
    s = Settings()
    assert s.instagram_app_id == "ig-123"
    assert s.instagram_app_secret == "ig-secret"


def test_instagram_meta_alias_env_names_are_read(monkeypatch):
    """The Meta App dashboard calls the values 'App ID' / 'App Secret'.
    Operators copy them as META_APP_ID / META_APP_SECRET. Code accepts."""
    monkeypatch.delenv("INSTAGRAM_APP_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    monkeypatch.setenv("META_APP_ID", "meta-456")
    monkeypatch.setenv("META_APP_SECRET", "meta-secret")
    s = Settings()
    assert s.instagram_app_id == "meta-456"
    assert s.instagram_app_secret == "meta-secret"


def test_instagram_facebook_alias_env_names_are_read(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_APP_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    monkeypatch.delenv("META_APP_ID", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.setenv("FACEBOOK_APP_ID", "fb-789")
    monkeypatch.setenv("FACEBOOK_APP_SECRET", "fb-secret")
    s = Settings()
    assert s.instagram_app_id == "fb-789"
    assert s.instagram_app_secret == "fb-secret"


def test_instagram_canonical_wins_when_both_are_set(monkeypatch):
    """If an operator sets both names, the canonical INSTAGRAM_* name
    takes precedence — that's the first entry in AliasChoices."""
    monkeypatch.setenv("INSTAGRAM_APP_ID", "ig-canonical")
    monkeypatch.setenv("META_APP_ID", "meta-fallback")
    s = Settings()
    assert s.instagram_app_id == "ig-canonical"


def test_instagram_empty_when_no_alias_is_set(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_APP_ID", raising=False)
    monkeypatch.delenv("INSTAGRAM_APP_SECRET", raising=False)
    monkeypatch.delenv("META_APP_ID", raising=False)
    monkeypatch.delenv("META_APP_SECRET", raising=False)
    monkeypatch.delenv("FACEBOOK_APP_ID", raising=False)
    monkeypatch.delenv("FACEBOOK_APP_SECRET", raising=False)
    s = Settings()
    assert s.instagram_app_id == ""
    assert s.instagram_app_secret == ""


def test_instagram_redirect_uri_alias(monkeypatch):
    monkeypatch.delenv("INSTAGRAM_REDIRECT_URI", raising=False)
    monkeypatch.setenv("META_REDIRECT_URI", "https://example.test/cb")
    s = Settings()
    assert s.instagram_redirect_uri == "https://example.test/cb"


def test_magic_link_callback_uses_public_app_url(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://internal.example")
    monkeypatch.setenv("PUBLIC_APP_URL", "https://www.babyg.ai/")
    s = Settings()
    assert s.magic_link_callback_url == "https://www.babyg.ai/auth/callback"


def test_magic_link_callback_accepts_site_url_alias(monkeypatch):
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
    monkeypatch.setenv("SITE_URL", "https://www.babyg.ai")
    s = Settings()
    assert s.magic_link_callback_url == "https://www.babyg.ai/auth/callback"


def test_magic_link_callback_falls_back_to_app_url(monkeypatch):
    monkeypatch.delenv("PUBLIC_APP_URL", raising=False)
    monkeypatch.delenv("SITE_URL", raising=False)
    monkeypatch.setenv("APP_URL", "http://localhost:8000/")
    s = Settings()
    assert s.magic_link_callback_url == "http://localhost:8000/auth/callback"
