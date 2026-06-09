"""Tavily integration tests.

The HTTP layer is mocked. These cover the contract the bot's
`_run_web_search` depends on: configured-vs-not, empty-query no-op,
parse safety, error mapping, and that the API key never appears in
log records.
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import get_settings
from app.integrations import tavily


@pytest.fixture(autouse=True)
def _reset_settings():
    """Each test starts from a clean Settings cache so monkeypatching
    TAVILY_API_KEY actually takes effect."""
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _fake_post(payload):
    """Return an httpx.post stub that captures the request and yields
    the supplied payload as a successful 200 response."""
    sent: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    def _post(url, json=None, timeout=None):
        sent["url"] = url
        sent["json"] = json
        sent["timeout"] = timeout
        return _Resp()

    return _post, sent


def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    get_settings.cache_clear()
    assert tavily.is_configured() is False
    monkeypatch.setenv("TAVILY_API_KEY", "tk-test")
    get_settings.cache_clear()
    assert tavily.is_configured() is True


def test_search_raises_not_configured_when_key_missing(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(tavily.TavilyNotConfiguredError):
        tavily.search("anything")


def test_search_returns_empty_for_blank_query(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tk-test")
    get_settings.cache_clear()
    # No HTTP call should be made — patch httpx.post to fail loudly.
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *a, **kw: pytest.fail("blank query should never hit Tavily"),
    )
    assert tavily.search("   ") == []


def test_search_parses_hits(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tk-test")
    get_settings.cache_clear()

    payload = {
        "results": [
            {
                "title": "Wynwood opening this weekend",
                "url": "https://example.com/wynwood",
                "content": "venue note...",
                "published_date": "2026-06-08",
            },
            {
                # Missing url — must be skipped, not crash.
                "title": "no-url row",
                "content": "ignored",
            },
            {
                "title": "Brand x creator news",
                "url": "https://example.com/brand",
                "content": "another snippet",
            },
        ]
    }
    post, sent = _fake_post(payload)
    monkeypatch.setattr(httpx, "post", post)

    hits = tavily.search("wynwood opening", max_results=3)

    assert len(hits) == 2
    assert hits[0].title == "Wynwood opening this weekend"
    assert hits[0].url == "https://example.com/wynwood"
    assert hits[0].snippet == "venue note..."
    assert hits[0].published == "2026-06-08"
    assert hits[1].published is None

    # The POST shape Tavily expects.
    assert sent["url"] == tavily.API_URL
    assert sent["json"]["api_key"] == "tk-test"
    assert sent["json"]["query"] == "wynwood opening"
    assert sent["json"]["max_results"] == 3


def test_search_clamps_max_results(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tk-test")
    get_settings.cache_clear()
    post, sent = _fake_post({"results": []})
    monkeypatch.setattr(httpx, "post", post)

    # Above the hard cap → clamped down.
    tavily.search("test", max_results=999)
    assert sent["json"]["max_results"] == tavily.HARD_MAX_RESULTS

    # Falsy (0 / None) → treated as "use the default", not "zero results"
    # (zero would be a no-op request, more confusing than helpful).
    tavily.search("test", max_results=0)
    assert sent["json"]["max_results"] == tavily.DEFAULT_MAX_RESULTS


def test_search_raises_call_error_on_http_failure(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tk-test")
    get_settings.cache_clear()

    class _Resp:
        status_code = 503

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", tavily.API_URL), response=self
            )

    def _post(*a, **kw):
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)
    with pytest.raises(tavily.TavilyCallError):
        tavily.search("hi")


def test_search_raises_call_error_on_json_decode_failure(monkeypatch):
    monkeypatch.setenv("TAVILY_API_KEY", "tk-test")
    get_settings.cache_clear()

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("not json")

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())
    with pytest.raises(tavily.TavilyCallError):
        tavily.search("hi")


def test_search_never_logs_api_key_or_query(monkeypatch, caplog):
    """Token logging is a hard constraint. Force a failure path so the
    error logger fires, then assert neither the key nor the query body
    leaked into a log record."""
    monkeypatch.setenv("TAVILY_API_KEY", "tk-supersecret-12345")
    get_settings.cache_clear()

    class _Resp:
        status_code = 500

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("POST", tavily.API_URL), response=self
            )

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _Resp())

    with caplog.at_level(logging.INFO), pytest.raises(tavily.TavilyCallError):
        tavily.search("private query about brand X")

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "tk-supersecret-12345" not in log_text
    assert "private query" not in log_text
