"""Shared pytest fixtures.

`_csrf_off` is autouse so every test in the suite POSTs without minting
a CSRF token. The middleware's behavior is exercised directly in
`tests/test_csrf.py`, which keeps the rest of the suite focused on
business logic.

`_clear_settings_cache` is autouse so the lru_cache'd Settings singleton
doesn't carry env mutations across tests.
"""

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import csrf
from app.core.rate_limit import (
    dm_brief_auto_limiter,
    dm_brief_manual_limiter,
    magic_link_limiter,
)
from app.main import app
from app.services import babyg_awareness


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Reset the in-process magic-link bucket between tests so a test that
    POSTs to /auth/magic-link or /auth/code six+ times doesn't starve the
    next test of the same IP's quota."""
    magic_link_limiter._buckets.clear()
    dm_brief_auto_limiter._buckets.clear()
    dm_brief_manual_limiter._buckets.clear()
    yield
    magic_link_limiter._buckets.clear()
    dm_brief_auto_limiter._buckets.clear()
    dm_brief_manual_limiter._buckets.clear()


@pytest.fixture(autouse=True)
def _clear_awareness_cache():
    """Reset the babyg-awareness snapshot cache between tests. The
    module-level cache is TTL'd in production; in tests, a snapshot
    built for u-1 by an earlier test would leak into the next test's
    fixture setup and mask its stubs."""
    babyg_awareness._CACHE.clear()
    yield
    babyg_awareness._CACHE.clear()


@pytest.fixture(autouse=True)
def _csrf_off(monkeypatch):
    async def _passthrough(self, scope, receive, send):
        await self.app(scope, receive, send)

    monkeypatch.setattr(csrf.CSRFMiddleware, "__call__", _passthrough)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)
