"""Instagram/Meta Graph API client tests.

HTTP is mocked at the httpx boundary. These cover the contract that
later phases (routes + bot tool) depend on:

  - configured detection
  - missing-config raises NotConfigured
  - exchange_code upgrades short-lived → long-lived in one call
  - resolve_business_account: success + no-pages + no-ig-account
  - media + insights parsing
  - error mapping for HTTP and JSON failures
  - app secret + access tokens NEVER appear in log records
"""

from __future__ import annotations

import logging

import httpx
import pytest

from app.config import get_settings
from app.integrations import instagram_meta


@pytest.fixture(autouse=True)
def _reset_settings():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _ok_response(payload):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return payload

    return _Resp()


def _err_response(status):
    class _Resp:
        def __init__(self):
            self.status_code = status

        def raise_for_status(self):
            raise httpx.HTTPStatusError(
                "boom",
                request=httpx.Request("GET", "https://graph.facebook.com/test"),
                response=self,
            )

    return _Resp()


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------


def test_is_configured_reflects_env(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "")
    get_settings.cache_clear()
    assert instagram_meta.is_configured() is False

    monkeypatch.setenv("INSTAGRAM_APP_ID", "test-app")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "test-secret")
    get_settings.cache_clear()
    assert instagram_meta.is_configured() is True


def test_auth_url_raises_when_not_configured(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "")
    get_settings.cache_clear()
    with pytest.raises(instagram_meta.InstagramNotConfiguredError):
        instagram_meta.auth_url("state")


def test_auth_url_includes_required_params(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    monkeypatch.setenv("APP_URL", "https://babyg.example")
    get_settings.cache_clear()

    url = instagram_meta.auth_url("st8")
    assert url.startswith(instagram_meta.AUTH_URL)
    assert "client_id=app-123" in url
    assert "state=st8" in url
    assert "response_type=code" in url
    assert "instagram_basic" in url
    assert "instagram_manage_insights" in url
    assert "pages_show_list" in url
    # default redirect_uri uses app_url + DEFAULT_CALLBACK_PATH
    assert "creator%2Finstagram%2Fcallback" in url


def test_redirect_uri_honors_env_override(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_REDIRECT_URI", "https://other.example/cb")
    get_settings.cache_clear()
    assert instagram_meta.redirect_uri() == "https://other.example/cb"


# ---------------------------------------------------------------------------
# exchange_code: short-lived → long-lived
# ---------------------------------------------------------------------------


def test_exchange_code_upgrades_to_long_lived(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()

    calls: list[dict] = []

    def _get(url, params=None, timeout=None):
        calls.append({"url": url, "params": dict(params or {})})
        if len(calls) == 1:
            # short-lived
            return _ok_response({"access_token": "short-token", "token_type": "bearer"})
        # long-lived exchange
        return _ok_response(
            {"access_token": "long-token", "token_type": "bearer", "expires_in": 5184000}
        )

    monkeypatch.setattr(httpx, "get", _get)
    out = instagram_meta.exchange_code("AUTH_CODE")

    assert out["access_token"] == "long-token"
    assert out["expires_in"] == 5184000
    # Two calls: first with `code`, second with grant_type=fb_exchange_token.
    assert calls[0]["params"]["code"] == "AUTH_CODE"
    assert calls[1]["params"]["grant_type"] == "fb_exchange_token"
    assert calls[1]["params"]["fb_exchange_token"] == "short-token"


def test_exchange_code_raises_when_short_lived_missing_token(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response({}))
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.exchange_code("AUTH_CODE")


def test_exchange_code_maps_http_error(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _err_response(500))
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.exchange_code("AUTH_CODE")


# ---------------------------------------------------------------------------
# resolve_business_account
# ---------------------------------------------------------------------------


def test_resolve_business_account_returns_first_eligible_account(monkeypatch):
    pages = {"data": [{"id": "page-A", "name": "Page A"}, {"id": "page-B", "name": "Page B"}]}
    page_b_detail = {
        "id": "page-B",
        "instagram_business_account": {
            "id": "1784140582230000",
            "username": "miacreates",
            "name": "Mia",
        },
    }

    seq = iter([_ok_response(pages), _ok_response({"id": "page-A"}), _ok_response(page_b_detail)])
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: next(seq))

    account = instagram_meta.resolve_business_account("TOKEN")
    assert account.ig_user_id == "1784140582230000"
    assert account.username == "miacreates"
    assert account.name == "Mia"


def test_resolve_business_account_raises_when_no_pages(monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **kw: _ok_response({"data": []})
    )
    with pytest.raises(instagram_meta.InstagramIneligibleAccountError):
        instagram_meta.resolve_business_account("TOKEN")


def test_resolve_business_account_raises_when_no_linked_ig(monkeypatch):
    pages = {"data": [{"id": "page-A", "name": "Page A"}]}
    page_detail_no_ig = {"id": "page-A"}  # no instagram_business_account block
    seq = iter([_ok_response(pages), _ok_response(page_detail_no_ig)])
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: next(seq))
    with pytest.raises(instagram_meta.InstagramIneligibleAccountError) as exc:
        instagram_meta.resolve_business_account("TOKEN")
    # Creator-facing copy must explain how to fix it.
    assert "Business or" in str(exc.value)
    assert "Facebook Page" in str(exc.value)


# ---------------------------------------------------------------------------
# get_user_media + get_media_insights
# ---------------------------------------------------------------------------


def test_get_user_media_parses_and_clamps_limit(monkeypatch):
    sent = {}

    def _get(url, params=None, timeout=None):
        sent["params"] = dict(params or {})
        return _ok_response(
            {
                "data": [
                    {
                        "id": "media-1",
                        "caption": "lunch at Boia",
                        "media_type": "IMAGE",
                        "permalink": "https://www.instagram.com/p/abc",
                        "timestamp": "2026-06-08T18:00:00+0000",
                        "like_count": 123,
                        "comments_count": 4,
                    },
                    # Missing id → skip, don't crash.
                    {"caption": "no id"},
                ]
            }
        )

    monkeypatch.setattr(httpx, "get", _get)
    media = instagram_meta.get_user_media("TOKEN", ig_user_id="ig-1", limit=999)
    assert len(media) == 1
    assert media[0].media_id == "media-1"
    assert media[0].like_count == 123
    # Hard-capped at MEDIA_HARD_MAX.
    assert sent["params"]["limit"] == str(instagram_meta.MEDIA_HARD_MAX)


def test_get_media_insights_returns_metric_map(monkeypatch):
    payload = {
        "data": [
            {"name": "engagement", "values": [{"value": 42}]},
            {"name": "reach", "values": [{"value": 1200}]},
            # `impressions` missing on purpose → comes back None
            {"name": "saved", "values": [{"value": 7}]},
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _ok_response(payload))

    out = instagram_meta.get_media_insights("TOKEN", media_id="media-1")
    assert out["engagement"] == 42
    assert out["reach"] == 1200
    assert out["saved"] == 7
    assert out["impressions"] is None


def test_graph_get_maps_http_error(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _err_response(503))
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.get_user_media("TOKEN", ig_user_id="ig-1")


def test_graph_get_maps_non_json_response(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            raise ValueError("nope")

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.get_user_media("TOKEN", ig_user_id="ig-1")


# ---------------------------------------------------------------------------
# token logging discipline (hard constraint)
# ---------------------------------------------------------------------------


def test_no_token_or_app_secret_in_logs_on_failure(monkeypatch, caplog):
    """Hard constraint: no token, app secret, or query body ever
    appears in a log record. Force a failure path on both the token
    endpoint and the Graph endpoint and assert nothing leaks."""
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-id-LEAK")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "app-secret-LEAK")
    get_settings.cache_clear()

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _err_response(500))

    with caplog.at_level(logging.INFO):
        with pytest.raises(instagram_meta.InstagramError):
            instagram_meta.exchange_code("CODE_LEAK")
        with pytest.raises(instagram_meta.InstagramError):
            instagram_meta.get_user_media("ACCESS_TOKEN_LEAK", ig_user_id="ig-1")

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "app-secret-LEAK" not in log_text
    assert "CODE_LEAK" not in log_text
    assert "ACCESS_TOKEN_LEAK" not in log_text
