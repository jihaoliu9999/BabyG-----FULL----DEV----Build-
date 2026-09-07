"""Inbound webhook endpoints for third-party providers.

Currently only Instagram. Design shape works for any Meta webhook
subscription (same GET-verify + POST-signed-JSON contract).

## Instagram DM webhook flow

1. **Meta Console setup (one-time, by an operator)**:
   Developer Console → your App → Products → Webhooks → Instagram
   → add callback URL `https://babyg.ai/webhooks/instagram` and a
   verify token that matches env `INSTAGRAM_WEBHOOK_VERIFY_TOKEN`.
   Subscribe the app to the `messages` field.

2. **Verify challenge (GET /webhooks/instagram)**:
   Meta hits us with `?hub.mode=subscribe&hub.verify_token=<x>&hub.challenge=<n>`.
   If `hub.verify_token` matches our env token, we echo back
   `hub.challenge` as plain text (Meta requires this — not JSON).
   Otherwise 403.

3. **Event delivery (POST /webhooks/instagram)**:
   Meta signs every POST body with HMAC-SHA256 using the app secret,
   in the `X-Hub-Signature-256` header (`sha256=<hex>`). We verify
   the signature BEFORE touching the payload. Signature miss = 403.
   Signature ok = hand off to `instagram_dms.ingest_webhook_payload`
   and return 200 immediately. Meta retries on any non-2xx, and our
   ingestion is idempotent per (creator_id, ig_message_id), so retry
   safety is baked in at the schema level too.

## What this file deliberately does NOT do

- No business logic. That's `instagram_dms.py` (slab #3).
- No token exchange. That's `oauth_connections.py`.
- No response body larger than needed. Meta only reads the status
  code + (on verify) the challenge text.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from app.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(tags=["webhooks"])


@router.get("/webhooks/instagram", include_in_schema=False)
async def instagram_verify(request: Request) -> Response:
    """Handle Meta's GET-based verify challenge.

    Meta sends this once when the operator clicks "Verify and Save"
    in the webhook subscription UI, and then any time the callback
    URL or verify token changes. Returns the raw `hub.challenge`
    value as plain text on match, 403 on mismatch. Empty
    verify-token env = every verify attempt fails (safe default).
    """
    settings = get_settings()
    expected = (settings.instagram_webhook_verify_token or "").strip()
    if not expected:
        logger.warning("instagram_webhook.verify.no_env_token_set")
        return PlainTextResponse("verify token not configured", status_code=403)

    mode = request.query_params.get("hub.mode", "")
    token = request.query_params.get("hub.verify_token", "")
    challenge = request.query_params.get("hub.challenge", "")

    if mode == "subscribe" and hmac.compare_digest(token, expected):
        logger.info("instagram_webhook.verify.ok")
        return PlainTextResponse(challenge, status_code=200)

    logger.warning(
        "instagram_webhook.verify.mismatch mode=%s token_len=%s",
        mode,
        len(token),
    )
    return PlainTextResponse("verification failed", status_code=403)


@router.post("/webhooks/instagram", include_in_schema=False)
async def instagram_event(request: Request) -> JSONResponse:
    """Handle Meta's POST-based webhook delivery.

    Order matters: read the raw body first, verify the signature
    against the raw bytes, THEN parse JSON. Parsing before verify
    would let a malicious payload allocate memory before we
    reject it.
    """
    settings = get_settings()
    app_secret = (settings.instagram_app_secret or "").strip()
    if not app_secret:
        logger.warning("instagram_webhook.event.no_app_secret_set")
        # Don't tell the caller why — just refuse.
        return JSONResponse({"ok": False}, status_code=403)

    raw_body = await request.body()
    header_sig = request.headers.get("x-hub-signature-256") or ""
    if not _verify_signature(app_secret, raw_body, header_sig):
        logger.warning(
            "instagram_webhook.event.bad_signature len=%s header_present=%s",
            len(raw_body),
            bool(header_sig),
        )
        return JSONResponse({"ok": False}, status_code=403)

    try:
        payload = await request.json()
    except Exception:
        logger.info("instagram_webhook.event.bad_json")
        # Ack anyway so Meta doesn't retry a permanently-broken body.
        return JSONResponse({"ok": True, "note": "unparseable"}, status_code=200)

    # Belt-and-suspenders: _dispatch_payload has its own try/except,
    # but if a future refactor removes it, we still don't want to
    # trigger Meta's retry-on-5xx storm. Ack + drop.
    try:
        _dispatch_payload(payload)
    except Exception:
        logger.exception("instagram_webhook.event.dispatch_raised_at_route")
    return JSONResponse({"ok": True}, status_code=200)


def _verify_signature(app_secret: str, raw_body: bytes, header: str) -> bool:
    """Constant-time HMAC-SHA256 check.

    Header format from Meta: `sha256=<hex>` (lowercase, 64 chars).
    Any deviation is rejected — no length hints leak because
    hmac.compare_digest is timing-safe.
    """
    prefix = "sha256="
    if not header.startswith(prefix):
        return False
    supplied_hex = header[len(prefix):].strip()
    if len(supplied_hex) != 64:
        return False
    expected_hex = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_hex, expected_hex)


def _dispatch_payload(payload: dict[str, Any]) -> None:
    """Route the verified payload into the right ingestion service.

    Slab #3 ships `app.services.instagram_dms.ingest_webhook_payload`.
    Until that lands, this is a no-op with a log line so the receiver
    is testable + observable in isolation. Never raises — Meta's
    retry-on-5xx behavior is expensive and drowns real errors, so
    ingestion failures degrade to a WARN, not a 500.
    """
    try:
        from app.services import instagram_dms
    except ImportError:
        logger.info("instagram_webhook.dispatch.service_missing_stub_mode")
        return
    try:
        instagram_dms.ingest_webhook_payload(payload)
    except Exception:
        logger.exception("instagram_webhook.dispatch.ingest_failed")
