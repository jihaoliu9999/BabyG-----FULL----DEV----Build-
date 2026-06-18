"""`_assert_app_url` must refuse to boot internet-reachable environments
with a misconfigured APP_URL.

Magic-link auth builds the Supabase callback as `{APP_URL}/auth/callback`.
If the value disagrees with Supabase Auth's redirect-allow-list (wrong
scheme, localhost leak from a forgotten .env, empty in CI), every signup
fails silently: users click the email and land on a Supabase redirect_uri
error, not on babyg. Catch the misconfiguration at boot so a bad deploy
crashes loudly instead of breaking signup quietly.
"""

from __future__ import annotations

import pytest

from app.main import _assert_app_url


class _StubSettings:
    def __init__(self, *, env: str, app_url: str) -> None:
        self.env = env
        self.app_url = app_url


# ---------- env=dev: guard MUST short-circuit ----------


def test_dev_accepts_localhost_http():
    """Local dev runs against http://localhost:8000; do not block contributors."""
    _assert_app_url(_StubSettings(env="dev", app_url="http://localhost:8000"))


def test_dev_accepts_empty_app_url():
    _assert_app_url(_StubSettings(env="dev", app_url=""))


# ---------- env=staging: must enforce ----------


def test_staging_rejects_empty():
    with pytest.raises(RuntimeError) as exc:
        _assert_app_url(_StubSettings(env="staging", app_url=""))
    assert "APP_URL" in str(exc.value)
    assert "staging" in str(exc.value)


def test_staging_rejects_http_scheme():
    with pytest.raises(RuntimeError) as exc:
        _assert_app_url(_StubSettings(env="staging", app_url="http://staging.babyg.ai"))
    assert "https" in str(exc.value)


def test_staging_rejects_localhost_hostname():
    with pytest.raises(RuntimeError) as exc:
        _assert_app_url(_StubSettings(env="staging", app_url="https://localhost:8000"))
    assert "localhost" in str(exc.value)


def test_staging_rejects_loopback_ipv4():
    with pytest.raises(RuntimeError) as exc:
        _assert_app_url(_StubSettings(env="staging", app_url="https://127.0.0.1"))
    assert "127.0.0.1" in str(exc.value)


def test_staging_rejects_url_without_host():
    with pytest.raises(RuntimeError):
        _assert_app_url(_StubSettings(env="staging", app_url="https://"))


def test_staging_accepts_canonical_https_url():
    _assert_app_url(_StubSettings(env="staging", app_url="https://staging.babyg.ai"))


def test_staging_accepts_trailing_slash():
    """Magic-link code strips trailing slashes; the guard shouldn't be stricter."""
    _assert_app_url(_StubSettings(env="staging", app_url="https://staging.babyg.ai/"))


# ---------- env=production: same guard ----------


def test_production_rejects_http_scheme():
    with pytest.raises(RuntimeError):
        _assert_app_url(_StubSettings(env="production", app_url="http://babyg.ai"))


def test_production_rejects_localhost():
    with pytest.raises(RuntimeError):
        _assert_app_url(
            _StubSettings(env="production", app_url="https://localhost:8000")
        )


def test_production_accepts_canonical_https_url():
    _assert_app_url(_StubSettings(env="production", app_url="https://babyg.ai"))
