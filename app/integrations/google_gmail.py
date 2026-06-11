"""Gmail v1 API client.

Read-only by construction. The module only exposes `list_threads` and
`get_thread`; no `send`, no `delete`, no `modify`, no draft. Slice 2
will add a separate `create_draft` surface — kept out of this module
to make the read/write split explicit at the file level.

Server-side only. Tokens never reach templates or browser JS.
Status-only logging: never logs the token, subject, body, recipients,
or any header values. Test pin asserts caplog records are clean.

Token comes from the existing Google OAuth row in `oauth_connections`
via `access_token_for_google`. This module is opaque to where the
token lives — callers hand it in and we make HTTP calls.
"""

from __future__ import annotations

import base64
import binascii
import logging
import re
from dataclasses import dataclass
from typing import Any, Final

import httpx

logger = logging.getLogger(__name__)

API_BASE: Final = "https://gmail.googleapis.com/gmail/v1/users/me"
TIMEOUT_SECONDS: Final = 20.0

# Inbox-facing safety rails. Per-tool caps live in bot.py; these are
# the per-call hard maxes so a runaway tool_input can never burn a
# huge chunk of the daily quota in a single call.
THREADS_HARD_MAX: Final = 20
THREADS_DEFAULT: Final = 5
MESSAGES_PER_THREAD: Final = 3
BODY_MAX_CHARS: Final = 1000

# Strip C0 control chars except tab/newline. Email bodies can carry
# weird unicode after MIME decoding; the bot doesn't need it and the
# tokens hurt.
_CONTROL_CHAR_RE: Final = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class GmailError(RuntimeError):
    """Generic Gmail API failure. Caller treats as 'inbox unavailable'."""


class GmailUnauthorizedError(GmailError):
    """401 from Gmail — token is expired or revoked. Caller may want
    to trigger a refresh via access_token_for_google."""


class GmailNotConnectedError(GmailError):
    """403 / scope-missing. The OAuth connection exists but lacks the
    gmail.readonly scope. Caller should surface 'not connected' to
    the user rather than 'upstream failed'."""


@dataclass(frozen=True)
class GmailMessage:
    """One message inside a thread, already sanitized + truncated."""

    message_id: str
    thread_id: str
    from_: str | None
    to: str | None
    subject: str | None
    snippet: str | None
    body_text: str | None
    internal_date: str | None
    is_unread: bool


@dataclass(frozen=True)
class GmailThread:
    """A thread plus its (capped, sanitized) recent messages."""

    thread_id: str
    snippet: str | None
    messages: list[GmailMessage]
    is_unread: bool


def list_thread_ids(token: str, *, limit: int = THREADS_DEFAULT) -> list[str]:
    """Return up to `limit` recent thread IDs from the primary inbox.

    The Gmail API's threads.list returns thread shells (id + snippet
    + historyId). We pull just the IDs and hand them to `get_thread`
    one at a time, so callers can short-circuit if needed.
    """
    bounded = max(1, min(int(limit or THREADS_DEFAULT), THREADS_HARD_MAX))
    payload = _get(
        token,
        f"{API_BASE}/threads",
        params={"maxResults": str(bounded), "labelIds": "INBOX"},
    )
    threads = payload.get("threads") or []
    return [str(t["id"]) for t in threads if isinstance(t, dict) and t.get("id")]


def get_thread(token: str, *, thread_id: str) -> GmailThread:
    """Fetch one thread with its messages, parsed + sanitized.

    Returns at most MESSAGES_PER_THREAD recent messages (newest at
    end of list per Gmail's chronological order). Each message body
    is text/plain only, control-char stripped, truncated.
    """
    payload = _get(
        token,
        f"{API_BASE}/threads/{thread_id}",
        params={"format": "full"},
    )
    raw_messages = payload.get("messages") or []
    if not isinstance(raw_messages, list):
        raw_messages = []
    # Keep the tail (newest) — Gmail returns oldest first.
    tail = raw_messages[-MESSAGES_PER_THREAD:]
    messages = [_parse_message(m, thread_id=thread_id) for m in tail]
    return GmailThread(
        thread_id=str(payload.get("id") or thread_id),
        snippet=_str_or_none(payload.get("snippet")),
        messages=messages,
        is_unread=any(m.is_unread for m in messages),
    )


def list_recent_threads(
    token: str, *, limit: int = THREADS_DEFAULT
) -> list[GmailThread]:
    """Convenience: list_thread_ids + get_thread for each.

    Bot tool uses this. One Graph-style failure aborts the whole call
    (caller sees `GmailError`). Per-thread failures don't drop the
    others — bad thread → skipped, others still returned.
    """
    ids = list_thread_ids(token, limit=limit)
    out: list[GmailThread] = []
    for tid in ids:
        try:
            out.append(get_thread(token, thread_id=tid))
        except GmailError:
            # One bad thread shouldn't kill the inbox view. Log status
            # only — never the thread id payload.
            logger.info("gmail thread fetch skipped (status-only)")
            continue
    return out


def _get(token: str, url: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
    """One Gmail GET. Maps HTTP status to the typed errors above."""
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=TIMEOUT_SECONDS) as client:
            response = client.get(url, headers=headers, params=params)
    except httpx.HTTPError:
        logger.exception("gmail request transport failure")
        raise GmailError("gmail request failed") from None

    if response.status_code == 401:
        logger.info("gmail 401 — token likely expired")
        raise GmailUnauthorizedError("gmail token unauthorized")
    if response.status_code == 403:
        logger.info("gmail 403 — likely missing scope")
        raise GmailNotConnectedError("gmail scope missing or insufficient")
    if response.status_code >= 400:
        logger.info("gmail http error %s", response.status_code)
        raise GmailError(f"gmail http {response.status_code}")

    try:
        return response.json()
    except ValueError:
        logger.exception("gmail response not json")
        raise GmailError("gmail response not json") from None


def _parse_message(raw: dict[str, Any], *, thread_id: str) -> GmailMessage:
    """Project the Gmail message JSON into a clean GmailMessage."""
    headers = _header_map(raw.get("payload"))
    labels = raw.get("labelIds") or []
    if not isinstance(labels, list):
        labels = []
    is_unread = "UNREAD" in labels
    body = _extract_plain_body(raw.get("payload"))
    return GmailMessage(
        message_id=str(raw.get("id") or ""),
        thread_id=thread_id,
        from_=headers.get("from"),
        to=headers.get("to"),
        subject=headers.get("subject"),
        snippet=_str_or_none(raw.get("snippet")),
        body_text=body,
        internal_date=_internal_date_iso(raw.get("internalDate")),
        is_unread=is_unread,
    )


def _header_map(payload: Any) -> dict[str, str]:
    """Lowercase-keyed map of the headers we care about."""
    if not isinstance(payload, dict):
        return {}
    headers = payload.get("headers") or []
    if not isinstance(headers, list):
        return {}
    wanted = {"from", "to", "cc", "subject", "date"}
    out: dict[str, str] = {}
    for h in headers:
        if not isinstance(h, dict):
            continue
        name = str(h.get("name") or "").lower()
        if name in wanted:
            out[name] = _sanitize(str(h.get("value") or ""))
    return out


def _extract_plain_body(payload: Any) -> str | None:
    """Walk MIME parts, return the first text/plain body sanitized + truncated.

    Walks both single-part and multipart structures. Skips text/html
    intentionally — html parts dilute the model's context with markup
    tokens; the plain part is enough for understanding the thread.
    """
    if not isinstance(payload, dict):
        return None
    mime = str(payload.get("mimeType") or "").lower()
    if mime == "text/plain":
        body = _decode_body(payload.get("body"))
        return body
    parts = payload.get("parts")
    if isinstance(parts, list):
        for part in parts:
            text = _extract_plain_body(part)
            if text:
                return text
    return None


def _decode_body(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, str) or not data:
        return None
    # Gmail uses URL-safe base64 with no padding.
    try:
        padded = data + "=" * (-len(data) % 4)
        raw = base64.urlsafe_b64decode(padded)
    except (binascii.Error, ValueError):
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return None
    sanitized = _sanitize(text)
    if len(sanitized) > BODY_MAX_CHARS:
        return sanitized[:BODY_MAX_CHARS].rstrip() + "…"
    return sanitized or None


def _sanitize(text: str) -> str:
    return _CONTROL_CHAR_RE.sub("", text).strip()


def _str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _internal_date_iso(value: Any) -> str | None:
    """Gmail's internalDate is a string of millis-since-epoch. Convert
    to ISO 8601 UTC so the bot can reason about it like other timestamps."""
    if value is None:
        return None
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return None
    from datetime import UTC, datetime

    return datetime.fromtimestamp(millis / 1000.0, UTC).isoformat()
