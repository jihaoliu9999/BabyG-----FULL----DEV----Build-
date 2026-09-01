"""Instagram Login API client tests.

HTTP is mocked at the httpx boundary. These cover the contract that
later phases (routes + bot tool) depend on:

  - configured detection
  - missing-config raises NotConfigured
  - auth_url uses instagram.com endpoint with the right scopes
  - exchange_code upgrades short-lived (POST) → long-lived (GET)
  - resolve_business_account: BUSINESS / MEDIA_CREATOR / PERSONAL
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
                request=httpx.Request("GET", "https://graph.instagram.com/test"),
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
    # Endpoint must be instagram.com — NOT facebook.com — for the
    # Instagram Login API flow.
    assert url.startswith("https://www.instagram.com/oauth/authorize")
    assert "client_id=app-123" in url
    assert "state=st8" in url
    assert "response_type=code" in url
    # New scope set. Old facebook-login scopes must NOT appear.
    assert "instagram_business_basic" in url
    assert "instagram_business_manage_insights" in url
    assert "pages_show_list" not in url
    assert "pages_read_engagement" not in url
    assert "instagram_basic%2C" not in url  # the old comma-joined old scope
    # default redirect_uri uses app_url + DEFAULT_CALLBACK_PATH
    assert "creator%2Finstagram%2Fcallback" in url


def test_redirect_uri_honors_env_override(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_REDIRECT_URI", "https://other.example/cb")
    get_settings.cache_clear()
    assert instagram_meta.redirect_uri() == "https://other.example/cb"


# ---------------------------------------------------------------------------
# exchange_code: short-lived (POST) → long-lived (GET)
# ---------------------------------------------------------------------------


def test_exchange_code_upgrades_to_long_lived(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()

    posts: list[dict] = []
    gets: list[dict] = []

    def _post(url, data=None, timeout=None):
        posts.append({"url": url, "data": dict(data or {})})
        return _ok_response(
            {
                "access_token": "short-token",
                "user_id": "1784140582230000",
            }
        )

    def _get(url, params=None, timeout=None):
        gets.append({"url": url, "params": dict(params or {})})
        return _ok_response(
            {
                "access_token": "long-token",
                "token_type": "bearer",
                "expires_in": 5184000,
            }
        )

    monkeypatch.setattr(httpx, "post", _post)
    monkeypatch.setattr(httpx, "get", _get)

    out = instagram_meta.exchange_code("AUTH_CODE")

    assert out["access_token"] == "long-token"
    assert out["expires_in"] == 5184000
    # IG user id surfaces forward from the short-lived response.
    assert out.get("user_id") == "1784140582230000"

    # Short-lived call: POST to api.instagram.com with grant_type=authorization_code
    assert len(posts) == 1
    assert posts[0]["url"] == "https://api.instagram.com/oauth/access_token"
    assert posts[0]["data"]["code"] == "AUTH_CODE"
    assert posts[0]["data"]["grant_type"] == "authorization_code"
    assert posts[0]["data"]["client_id"] == "app-123"

    # Long-lived exchange: GET to graph.instagram.com with ig_exchange_token
    assert len(gets) == 1
    assert gets[0]["url"] == "https://graph.instagram.com/access_token"
    assert gets[0]["params"]["grant_type"] == "ig_exchange_token"
    assert gets[0]["params"]["access_token"] == "short-token"


def test_exchange_code_raises_when_short_lived_missing_token(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _ok_response({}))
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: pytest.fail("must not call long-lived when short-lived empty"),
    )
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.exchange_code("AUTH_CODE")


def test_exchange_code_maps_http_error(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _err_response(500))
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.exchange_code("AUTH_CODE")


def test_refresh_long_lived_token_uses_ig_refresh_grant(monkeypatch):
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-123")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "secret")
    get_settings.cache_clear()
    captured: dict = {}

    def _get(url, params=None, timeout=None):
        captured["url"] = url
        captured["params"] = dict(params or {})
        return _ok_response(
            {"access_token": "refreshed-token", "expires_in": 5184000}
        )

    monkeypatch.setattr(httpx, "get", _get)
    out = instagram_meta.refresh_long_lived_token("existing-token")
    assert out["access_token"] == "refreshed-token"
    assert captured["url"] == "https://graph.instagram.com/refresh_access_token"
    assert captured["params"]["grant_type"] == "ig_refresh_token"
    assert captured["params"]["access_token"] == "existing-token"


# ---------------------------------------------------------------------------
# resolve_business_account — /me?fields=account_type
# ---------------------------------------------------------------------------


def test_resolve_business_account_accepts_business_type(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _ok_response(
            {
                "id": "1784140582230000",
                "username": "miacreates",
                "name": "Mia",
                "account_type": "BUSINESS",
            }
        ),
    )
    account = instagram_meta.resolve_business_account("TOKEN")
    assert account.ig_user_id == "1784140582230000"
    assert account.username == "miacreates"
    assert account.name == "Mia"


def test_resolve_business_account_accepts_media_creator_type(monkeypatch):
    """Creator accounts (MEDIA_CREATOR) are the IG-app-side equivalent
    of a Business account for our purposes — same insights surface."""
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _ok_response(
            {"id": "ig-99", "username": "c", "account_type": "MEDIA_CREATOR"}
        ),
    )
    account = instagram_meta.resolve_business_account("TOKEN")
    assert account.ig_user_id == "ig-99"


def test_resolve_business_account_refuses_personal(monkeypatch):
    """Personal IG accounts authorize but get refused at this step.
    The route surfaces the specific copy and does NOT save a row."""
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _ok_response(
            {"id": "ig-1", "username": "u", "account_type": "PERSONAL"}
        ),
    )
    with pytest.raises(instagram_meta.InstagramIneligibleAccountError) as exc:
        instagram_meta.resolve_business_account("TOKEN")
    # Creator-facing copy must explain what to do.
    assert "Business or" in str(exc.value)
    assert "Creator account" in str(exc.value)
    # New flow does NOT mention Facebook Pages — that requirement is gone.
    assert "Facebook Page" not in str(exc.value)


def test_resolve_business_account_refuses_unknown_account_type(monkeypatch):
    """Defensive: an unexpected account_type value (or missing field)
    must refuse rather than accept by default."""
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _ok_response({"id": "ig-1", "username": "u"}),
    )
    with pytest.raises(instagram_meta.InstagramIneligibleAccountError):
        instagram_meta.resolve_business_account("TOKEN")


def test_resolve_business_account_raises_when_me_missing_id(monkeypatch):
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **kw: _ok_response({"account_type": "BUSINESS"}),
    )
    with pytest.raises(instagram_meta.InstagramError):
        instagram_meta.resolve_business_account("TOKEN")


# ---------------------------------------------------------------------------
# get_user_media + get_media_insights
# ---------------------------------------------------------------------------


def test_get_user_media_parses_and_clamps_limit(monkeypatch):
    sent = {}

    def _get(url, params=None, timeout=None):
        sent["url"] = url
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
    # Endpoint host must be graph.instagram.com now.
    assert sent["url"].startswith("https://graph.instagram.com/")


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


# ---------------------------------------------------------------------------
# get_account_snapshot — Phase B
# ---------------------------------------------------------------------------


def test_get_account_snapshot_combines_totals_and_insights(monkeypatch):
    """Two Graph calls: /{ig_user_id} for totals + /{ig_user_id}/insights
    for daily metrics. Both parsed into one flat dict; the insights
    "last day" wins over earlier days in the values array."""
    calls: list[tuple[str, dict]] = []

    def _get(url, params=None, timeout=None):
        calls.append((url, dict(params or {})))
        if url.endswith("/ig-42/insights"):
            return _ok_response(
                {
                    "data": [
                        {
                            "name": "reach",
                            "values": [
                                {"value": 900},   # older day
                                {"value": 1100},  # freshest day → wins
                            ],
                        },
                        {"name": "profile_views", "values": [{"value": 55}]},
                        # `impressions` deliberately absent → None
                    ]
                }
            )
        return _ok_response(
            {
                "followers_count": 12340,
                "follows_count": 210,
                "media_count": 87,
            }
        )

    monkeypatch.setattr(httpx, "get", _get)
    snap = instagram_meta.get_account_snapshot("TOKEN", ig_user_id="ig-42")
    assert snap["followers_count"] == 12340
    assert snap["follows_count"] == 210
    assert snap["media_count"] == 87
    assert snap["reach"] == 1100
    assert snap["profile_views"] == 55
    assert snap["impressions"] is None
    # Two calls fired: totals then insights.
    assert calls[0][0].endswith("/ig-42")
    assert calls[1][0].endswith("/ig-42/insights")
    assert calls[1][1]["period"] == "day"


def test_get_account_snapshot_survives_totals_failure(monkeypatch):
    """A failure on the totals call must NOT nuke the insights call;
    partial data is what the caller renders."""

    def _get(url, params=None, timeout=None):
        if url.endswith("/insights"):
            return _ok_response(
                {"data": [{"name": "reach", "values": [{"value": 500}]}]}
            )
        return _err_response(500)

    monkeypatch.setattr(httpx, "get", _get)
    snap = instagram_meta.get_account_snapshot("TOKEN", ig_user_id="ig-42")
    assert snap["followers_count"] is None  # totals call failed
    assert snap["reach"] == 500  # insights call still landed


def test_get_account_snapshot_survives_insights_failure(monkeypatch):
    """And the inverse — totals land, insights fail."""

    def _get(url, params=None, timeout=None):
        if url.endswith("/insights"):
            return _err_response(500)
        return _ok_response({"followers_count": 9999, "follows_count": 5})

    monkeypatch.setattr(httpx, "get", _get)
    snap = instagram_meta.get_account_snapshot("TOKEN", ig_user_id="ig-42")
    assert snap["followers_count"] == 9999
    assert snap["follows_count"] == 5
    assert snap["reach"] is None
    assert snap["impressions"] is None
    assert snap["profile_views"] is None


def test_account_snapshot_reads_only_scope_tools(monkeypatch):
    """Belt-and-braces: the module must not have gained a write tool
    while we were wiring account insights. If a future edit adds
    e.g. content_publish, this test flags it before the tokens reach
    users."""
    forbidden = {"content_publish", "manage_comments", "manage_messages"}
    for scope in instagram_meta.SCOPES:
        assert scope not in forbidden, scope


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
    """Hard constraint: no token, app secret, code, or query body
    appears in a log record. Force failure paths on both the token
    endpoints and the Graph endpoint and assert nothing leaks."""
    monkeypatch.setenv("INSTAGRAM_APP_ID", "app-id-LEAK")
    monkeypatch.setenv("INSTAGRAM_APP_SECRET", "app-secret-LEAK")
    get_settings.cache_clear()

    monkeypatch.setattr(httpx, "post", lambda *a, **kw: _err_response(500))
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _err_response(500))

    with caplog.at_level(logging.INFO):
        with pytest.raises(instagram_meta.InstagramError):
            instagram_meta.exchange_code("CODE_LEAK")
        with pytest.raises(instagram_meta.InstagramError):
            instagram_meta.get_user_media("ACCESS_TOKEN_LEAK", ig_user_id="ig-1")
        with pytest.raises(instagram_meta.InstagramError):
            instagram_meta.refresh_long_lived_token("REFRESH_TOKEN_LEAK")

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "app-secret-LEAK" not in log_text
    assert "CODE_LEAK" not in log_text
    assert "ACCESS_TOKEN_LEAK" not in log_text
    assert "REFRESH_TOKEN_LEAK" not in log_text
