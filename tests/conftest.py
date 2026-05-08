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
from app.main import app


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _csrf_off(monkeypatch):
    async def _passthrough(self, scope, receive, send):
        await self.app(scope, receive, send)

    monkeypatch.setattr(csrf.CSRFMiddleware, "__call__", _passthrough)


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)
