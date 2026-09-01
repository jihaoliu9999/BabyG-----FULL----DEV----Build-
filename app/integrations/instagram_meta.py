"""Instagram Login API client (Meta's v22-era flow).

This module talks to the **Instagram Login API**, not the older
Facebook-Login → Instagram-Graph-via-linked-Page path. The difference
matters because:

  - OAuth endpoint is instagram.com, not facebook.com
  - Scopes are the `instagram_business_*` family
  - The token authorizes the IG user directly — no FB Page traversal
  - The account must still be an Instagram Business or Creator account,
    but it does NOT need to be linked to a Facebook Page

Server-side only. Tokens never reach templates or browser JS.
Status-only logging — never logs tokens, app secret, query bodies,
or response payloads.

Scope is strictly read-only: instagram_business_basic +
instagram_business_manage_insights. We never request the publish,
comments, or messages scopes — the module surface itself doesn't
expose write functions (test-pinned in tests/test_instagram_routes.py).

Eligibility: only Instagram Business or Creator accounts qualify.
A Personal account that authorizes will surface as
InstagramIneligibleAccountError so the route can refuse to save a
connection row — no fake connected state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

# Instagram Login API uses three host bases:
#   - instagram.com   for the consent UI
#   - api.instagram.com for the short-lived token exchange
#   - graph.instagram.com for long-lived tokens + business data + insights
GRAPH_VERSION: Final = "v22.0"
AUTH_URL: Final = "https://www.instagram.com/oauth/authorize"
SHORT_LIVED_TOKEN_URL: Final = "https://api.instagram.com/oauth/access_token"
LONG_LIVED_TOKEN_URL: Final = "https://graph.instagram.com/access_token"
REFRESH_TOKEN_URL: Final = "https://graph.instagram.com/refresh_access_token"
GRAPH_BASE: Final = f"https://graph.instagram.com/{GRAPH_VERSION}"
DEFAULT_CALLBACK_PATH: Final = "/creator/instagram/callback"

# Read-only scope set. instagram_business_basic alone covers profile +
# media listing; insights is added so we can pull engagement/reach/etc.
# We deliberately do NOT include content_publish, manage_comments, or
# manage_messages — those would grant write capabilities our integration
# module never exposes.
SCOPES: Final[tuple[str, ...]] = (
    "instagram_business_basic",
    "instagram_business_manage_insights",
)

TIMEOUT_SECONDS: Final = 20.0
MEDIA_DEFAULT_LIMIT: Final = 5
MEDIA_HARD_MAX: Final = 25
MEDIA_FIELDS: Final = (
    "id,caption,media_type,media_product_type,permalink,"
    "timestamp,like_count,comments_count"
)
INSIGHT_METRICS: Final[tuple[str, ...]] = (
    "engagement",
    "impressions",
    "reach",
    "saved",
)
# Account-level daily insights. Split from INSIGHT_METRICS because Meta
# scopes these to the /{ig_user_id}/insights endpoint (period=day), not
# the per-media /{media_id}/insights endpoint. Kept short + stable to
# avoid churn as Meta deprecates individual metrics across API versions.
ACCOUNT_INSIGHT_METRICS: Final[tuple[str, ...]] = (
    "reach",
    "impressions",
    "profile_views",
)
# Account totals live on /me?fields=... instead of /insights. followers_count
# from /me is the current total; period=day on /insights would give daily
# NEW followers which is a different number. We surface totals here because
# growth is computed by lag over the daily-snapshot table in Phase C.
ACCOUNT_TOTAL_FIELDS: Final[tuple[str, ...]] = (
    "followers_count",
    "follows_count",
    "media_count",
)

# Account types Instagram returns from /me?fields=account_type.
# Only the first two pass eligibility.
_ELIGIBLE_ACCOUNT_TYPES: Final[frozenset[str]] = frozenset(
    {"BUSINESS", "MEDIA_CREATOR"}
)


class InstagramError(RuntimeError):
    """Generic non-secret Instagram API failure (network, 5xx, parse)."""


class InstagramNotConfiguredError(RuntimeError):
    """Raised when INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET are unset."""


class InstagramIneligibleAccountError(RuntimeError):
    """The creator authenticated, but their account is Personal (not
    Business or Creator). The route surfaces this as a specific copy
    so the creator knows how to fix it; the connection row is NOT saved."""


@dataclass(frozen=True)
class InstagramAccount:
    ig_user_id: str
    username: str | None
    name: str | None


@dataclass(frozen=True)
class InstagramMedia:
    media_id: str
    caption: str | None
    media_type: str | None
    permalink: str | None
    timestamp: str | None
    like_count: int | None
    comments_count: int | None


def is_configured() -> bool:
    settings = get_settings()
    return bool(settings.instagram_app_id and settings.instagram_app_secret)


def redirect_uri() -> str:
    settings = get_settings()
    if settings.instagram_redirect_uri:
        return settings.instagram_redirect_uri
    return f"{settings.app_url.rstrip('/')}{DEFAULT_CALLBACK_PATH}"


def scopes() -> list[str]:
    return list(SCOPES)


def auth_url(state: str) -> str:
    """Build the instagram.com consent URL.

    Scopes are comma-separated per the Instagram Login API spec. Note
    that this is the bare instagram.com endpoint — we do not go through
    facebook.com here.
    """
    if not is_configured():
        raise InstagramNotConfiguredError("Instagram OAuth is not configured")
    params = {
        "client_id": get_settings().instagram_app_id,
        "redirect_uri": redirect_uri(),
        "response_type": "code",
        "scope": ",".join(SCOPES),
        "state": state,
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str) -> dict[str, Any]:
    """Exchange auth code → short-lived token → long-lived (60d).

    Two-step on the Instagram Login API:
      1. POST to api.instagram.com with the auth code (short-lived)
      2. GET graph.instagram.com to exchange short → long-lived
    """
    if not is_configured():
        raise InstagramNotConfiguredError("Instagram OAuth is not configured")
    settings = get_settings()
    short_lived = _post_short_lived_token(
        {
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri(),
            "code": code,
        }
    )
    short_token = str(short_lived.get("access_token") or "")
    if not short_token:
        raise InstagramError("Instagram token exchange missing access_token")
    long_lived = _exchange_long_lived(short_token)
    # Surface the IG user id from the short-lived response so the route
    # doesn't need a separate /me call just for the id during signup —
    # but defensively, callers should still treat /me as the source of
    # truth for the IG user id (it's what resolve_business_account does).
    if "user_id" not in long_lived and short_lived.get("user_id"):
        long_lived["user_id"] = short_lived["user_id"]
    return long_lived


def refresh_long_lived_token(token: str) -> dict[str, Any]:
    """Refresh a long-lived IG token via the ig_refresh_token grant.

    Long-lived IG tokens are valid for ~60 days; calling this within
    that window mints a new 60-day token. After expiry the user has
    to reconnect.
    """
    if not is_configured():
        raise InstagramNotConfiguredError("Instagram OAuth is not configured")
    return _get_token_via(
        REFRESH_TOKEN_URL,
        {
            "grant_type": "ig_refresh_token",
            "access_token": token,
        },
    )


def resolve_business_account(access_token: str) -> InstagramAccount:
    """Fetch the IG user's profile and refuse Personal accounts.

    With the Instagram Login API the token itself authorizes the IG
    user directly, so we just call /me with the right fields and
    inspect account_type. No FB Page traversal step.
    """
    detail = _graph_get(
        "/me",
        access_token,
        params={"fields": "id,username,name,account_type"},
    )
    ig_user_id = str(detail.get("id") or "").strip()
    if not ig_user_id:
        raise InstagramError("Instagram /me response missing id")
    account_type = str(detail.get("account_type") or "").strip().upper()
    if account_type not in _ELIGIBLE_ACCOUNT_TYPES:
        raise InstagramIneligibleAccountError(
            "Instagram connection requires an Instagram Business or "
            "Creator account."
        )
    return InstagramAccount(
        ig_user_id=ig_user_id,
        username=_str_or_none(detail.get("username"), max_len=80),
        name=_str_or_none(detail.get("name"), max_len=120),
    )


def get_user_media(
    access_token: str,
    *,
    ig_user_id: str,
    limit: int = MEDIA_DEFAULT_LIMIT,
) -> list[InstagramMedia]:
    """List recent media for the IG user.

    `ig_user_id` is the value returned by resolve_business_account.
    Instagram Login API also accepts "me" here, but we pass the explicit
    id so multi-account flows (future) stay correct.
    """
    bounded = max(1, min(int(limit or MEDIA_DEFAULT_LIMIT), MEDIA_HARD_MAX))
    data = _graph_get(
        f"/{ig_user_id}/media",
        access_token,
        params={"fields": MEDIA_FIELDS, "limit": str(bounded)},
    )
    rows = data.get("data") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    out: list[InstagramMedia] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        media_id = str(row.get("id") or "").strip()
        if not media_id:
            continue
        out.append(
            InstagramMedia(
                media_id=media_id,
                caption=_str_or_none(row.get("caption"), max_len=600),
                media_type=_str_or_none(row.get("media_type"), max_len=40),
                permalink=_str_or_none(row.get("permalink"), max_len=600),
                timestamp=_str_or_none(row.get("timestamp"), max_len=40),
                like_count=_int_or_none(row.get("like_count")),
                comments_count=_int_or_none(row.get("comments_count")),
            )
        )
    return out


def get_media_insights(access_token: str, *, media_id: str) -> dict[str, int | None]:
    """Returns {metric: value} for the configured metric set.

    Not every media type supports every metric (stories vs feed vs
    reels diverge). Missing values come back as None rather than
    raising — the caller renders only what's available.
    """
    data = _graph_get(
        f"/{media_id}/insights",
        access_token,
        params={"metric": ",".join(INSIGHT_METRICS)},
    )
    rows = data.get("data") if isinstance(data, dict) else None
    out: dict[str, int | None] = dict.fromkeys(INSIGHT_METRICS)
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if name not in out:
            continue
        values = row.get("values")
        if not isinstance(values, list) or not values:
            continue
        first = values[0]
        if not isinstance(first, dict):
            continue
        out[name] = _int_or_none(first.get("value"))
    return out


def get_account_snapshot(
    access_token: str, *, ig_user_id: str
) -> dict[str, int | None]:
    """Point-in-time account snapshot.

    Combines two Graph calls:
      - GET /{ig_user_id}?fields=followers_count,follows_count,media_count
        → current totals (stable across Meta API versions).
      - GET /{ig_user_id}/insights?metric=reach,impressions,profile_views&period=day
        → the latest day of processed insights (last full day per Meta).

    Returns one flat dict keyed by ACCOUNT_TOTAL_FIELDS +
    ACCOUNT_INSIGHT_METRICS. Any individual field that Meta refuses or
    returns malformed is surfaced as None so the caller can render
    partial data instead of a hard error; the two Graph calls are
    isolated so a failure on one does not blank the other.
    """
    out: dict[str, int | None] = dict.fromkeys(
        ACCOUNT_TOTAL_FIELDS + ACCOUNT_INSIGHT_METRICS
    )

    try:
        totals = _graph_get(
            f"/{ig_user_id}",
            access_token,
            params={"fields": ",".join(ACCOUNT_TOTAL_FIELDS)},
        )
    except InstagramError:
        totals = {}
    for field in ACCOUNT_TOTAL_FIELDS:
        out[field] = _int_or_none(totals.get(field))

    try:
        insights = _graph_get(
            f"/{ig_user_id}/insights",
            access_token,
            params={
                "metric": ",".join(ACCOUNT_INSIGHT_METRICS),
                "period": "day",
            },
        )
    except InstagramError:
        return out
    rows = insights.get("data") if isinstance(insights, dict) else None
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "")
        if name not in ACCOUNT_INSIGHT_METRICS:
            continue
        values = row.get("values")
        if not isinstance(values, list) or not values:
            continue
        # Meta returns one value per day in period=day, chronological.
        # The last entry is the freshest fully-processed day.
        last = values[-1]
        if not isinstance(last, dict):
            continue
        out[name] = _int_or_none(last.get("value"))
    return out


def _post_short_lived_token(payload: dict[str, str]) -> dict[str, Any]:
    """Short-lived token exchange is POST form-encoded per the
    Instagram Login API spec."""
    try:
        response = httpx.post(
            SHORT_LIVED_TOKEN_URL, data=payload, timeout=TIMEOUT_SECONDS
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        logger.info("Instagram short-lived token call failed with status %s", status)
        raise InstagramError("Instagram short-lived token request failed") from exc
    try:
        data = response.json()
    except ValueError as exc:
        logger.info("Instagram short-lived token response was not JSON")
        raise InstagramError(
            "Instagram short-lived token response was not JSON"
        ) from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise InstagramError("Instagram short-lived response missing access_token")
    return data


def _exchange_long_lived(short_token: str) -> dict[str, Any]:
    settings = get_settings()
    return _get_token_via(
        LONG_LIVED_TOKEN_URL,
        {
            "grant_type": "ig_exchange_token",
            "client_secret": settings.instagram_app_secret,
            "access_token": short_token,
        },
    )


def _get_token_via(url: str, payload: dict[str, str]) -> dict[str, Any]:
    """Long-lived exchange and refresh are GET with query params."""
    try:
        response = httpx.get(url, params=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        # Status only. Never log app secret or token.
        logger.info("Instagram long-lived token call failed with status %s", status)
        raise InstagramError("Instagram long-lived token request failed") from exc
    try:
        data = response.json()
    except ValueError as exc:
        logger.info("Instagram long-lived token response was not JSON")
        raise InstagramError(
            "Instagram long-lived token response was not JSON"
        ) from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise InstagramError(
            "Instagram long-lived response missing access_token"
        )
    return data


def _graph_get(
    path: str,
    access_token: str,
    *,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    query = {"access_token": access_token, **(params or {})}
    url = f"{GRAPH_BASE}{path}"
    try:
        response = httpx.get(url, params=query, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        # Status only. Never log the access_token or any query body.
        logger.info("Instagram Graph GET %s failed with status %s", path, status)
        raise InstagramError(f"Instagram Graph request failed: {path}") from exc
    try:
        data = response.json()
    except ValueError as exc:
        logger.info("Instagram Graph %s returned non-JSON", path)
        raise InstagramError(
            f"Instagram Graph response was not JSON: {path}"
        ) from exc
    if not isinstance(data, dict):
        raise InstagramError(f"Instagram Graph response was not an object: {path}")
    return data


def _str_or_none(value: Any, *, max_len: int) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    return s[:max_len]


def _int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
