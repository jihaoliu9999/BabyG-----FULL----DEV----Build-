"""Instagram/Meta Graph API client.

Server-side only. Tokens never reach templates or browser JS.
Status-only logging — never logs tokens, app secret, query bodies,
or response payloads.

Scope is strictly read-only: instagram_basic + instagram_manage_insights
plus the two Page scopes needed to traverse FB Page → IG Business
Account. None of these grant publishing, DMs, comments, or any write.
That's the OAuth-layer guarantee against the hard constraints; the
service layer doesn't even expose a write endpoint.

Eligibility: only Instagram Business or Creator accounts that are
linked to a Facebook Page can be connected. Personal IG accounts and
unlinked accounts surface as InstagramIneligibleAccountError so the
route can refuse to save a connection row — no fake connected state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

GRAPH_VERSION: Final = "v19.0"
GRAPH_BASE: Final = f"https://graph.facebook.com/{GRAPH_VERSION}"
AUTH_URL: Final = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
TOKEN_URL: Final = f"{GRAPH_BASE}/oauth/access_token"
DEFAULT_CALLBACK_PATH: Final = "/creator/instagram/callback"

SCOPES: Final[tuple[str, ...]] = (
    "instagram_basic",
    "instagram_manage_insights",
    "pages_show_list",
    "pages_read_engagement",
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


class InstagramError(RuntimeError):
    """Generic non-secret IG Graph API failure (network, 5xx, parse)."""


class InstagramNotConfiguredError(RuntimeError):
    """Raised when INSTAGRAM_APP_ID / INSTAGRAM_APP_SECRET are unset."""


class InstagramIneligibleAccountError(RuntimeError):
    """The creator authenticated, but their account doesn't qualify:
    no Facebook Pages, or no Page-linked Instagram Business/Creator
    account. The route surfaces this as a specific copy so the
    creator knows how to fix it; the connection row is NOT saved."""


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
    """Exchange auth code → short-lived access token → long-lived (60d)."""
    if not is_configured():
        raise InstagramNotConfiguredError("Instagram OAuth is not configured")
    settings = get_settings()
    short_lived = _get_token(
        {
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "redirect_uri": redirect_uri(),
            "code": code,
        }
    )
    short_token = str(short_lived.get("access_token") or "")
    if not short_token:
        raise InstagramError("Instagram token exchange missing access_token")
    # Immediately upgrade to a long-lived token so we don't have to
    # re-prompt the user every hour. The long-lived token expires in
    # ~60 days and is refreshed via the same fb_exchange_token call.
    return refresh_long_lived_token(short_token)


def refresh_long_lived_token(token: str) -> dict[str, Any]:
    if not is_configured():
        raise InstagramNotConfiguredError("Instagram OAuth is not configured")
    settings = get_settings()
    return _get_token(
        {
            "grant_type": "fb_exchange_token",
            "client_id": settings.instagram_app_id,
            "client_secret": settings.instagram_app_secret,
            "fb_exchange_token": token,
        }
    )


def resolve_business_account(access_token: str) -> InstagramAccount:
    """Traverse FB Pages → linked IG Business Account.

    Returns the first eligible IG Business/Creator account. Raises
    InstagramIneligibleAccountError when the user has no Pages, no
    Page has a linked IG Business Account, or the linked account is
    personal. The route layer surfaces a creator-facing message that
    explains the requirement.
    """
    pages = _graph_get("/me/accounts", access_token, params={"fields": "id,name"})
    page_rows = pages.get("data") if isinstance(pages, dict) else None
    if not isinstance(page_rows, list) or not page_rows:
        raise InstagramIneligibleAccountError(
            "Instagram connection requires an Instagram Business or "
            "Creator account linked to a Facebook Page."
        )
    for page in page_rows:
        if not isinstance(page, dict):
            continue
        page_id = str(page.get("id") or "").strip()
        if not page_id:
            continue
        detail = _graph_get(
            f"/{page_id}",
            access_token,
            params={"fields": "instagram_business_account{id,username,name}"},
        )
        ig_block = detail.get("instagram_business_account") if isinstance(detail, dict) else None
        if not isinstance(ig_block, dict):
            continue
        ig_user_id = str(ig_block.get("id") or "").strip()
        if not ig_user_id:
            continue
        return InstagramAccount(
            ig_user_id=ig_user_id,
            username=(str(ig_block.get("username")).strip() or None)
            if ig_block.get("username")
            else None,
            name=(str(ig_block.get("name")).strip() or None)
            if ig_block.get("name")
            else None,
        )
    raise InstagramIneligibleAccountError(
        "Instagram connection requires an Instagram Business or "
        "Creator account linked to a Facebook Page."
    )


def get_user_media(
    access_token: str,
    *,
    ig_user_id: str,
    limit: int = MEDIA_DEFAULT_LIMIT,
) -> list[InstagramMedia]:
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


def _get_token(payload: dict[str, str]) -> dict[str, Any]:
    """Token endpoint is GET with query params per Meta's docs.
    POST body works too but GET matches their reference flow."""
    try:
        response = httpx.get(TOKEN_URL, params=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        # Status only. Never log app secret, code, or token body.
        logger.info("Instagram OAuth token call failed with status %s", status)
        raise InstagramError("Instagram OAuth token request failed") from exc
    try:
        data = response.json()
    except ValueError as exc:
        logger.info("Instagram OAuth token response was not JSON")
        raise InstagramError("Instagram OAuth token response was not JSON") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise InstagramError("Instagram OAuth response missing access_token")
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
        raise InstagramError(f"Instagram Graph response was not JSON: {path}") from exc
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
