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
    logger.info(
        "instagram_webhook.event.received body_len=%s signature_present=%s",
        len(raw_body),
        bool(header_sig),
    )
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

    entries = payload.get("entry") if isinstance(payload, dict) else None
    logger.info(
        "instagram_webhook.event.parsed object=%s entries=%s",
        payload.get("object") if isinstance(payload, dict) else type(payload).__name__,
        len(entries) if isinstance(entries, list) else "invalid",
    )
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


# ---------------------------------------------------------------------------
# Stripe — Connect (sandbox first)
#
# This is the receiving foundation only. It verifies Stripe's signature
# on every POST using STRIPE_WEBHOOK_SECRET via stripe.Webhook.construct_event
# (Stripe's own official verification path — same code as their docs) and
# acknowledges the event with a 2xx. It intentionally does not run any
# business logic yet: no DB writes, no money movement, no application
# actions. Only the event id + event type are logged, so no card, bank,
# customer, or personal data ever reaches our logs from this receiver.
#
# Unknown event types are also acknowledged with 200 so Stripe does not
# retry them forever while their handlers are still being implemented.
# ---------------------------------------------------------------------------


@router.post("/webhooks/stripe", include_in_schema=False)
async def stripe_event(request: Request) -> Response:
    """Receive one Stripe webhook, verify its signature, and ack.

    Failure modes (all constant-time from the caller's perspective):
      * secret unconfigured on this deploy         → 503
      * missing Stripe-Signature header             → 400
      * malformed body (unparseable / truncated)    → 400
      * signature mismatch                          → 400
      * Stripe SDK not importable                   → 503
      * anything else raised inside signature check → 400

    On success the response is `{"received": true, "event_id": <str>}`
    with a 200. That is deliberately the shape Stripe's own docs
    recommend so their retry logic and their dashboard's delivery
    view read the endpoint as healthy.
    """
    settings = get_settings()
    webhook_secret = (settings.stripe_webhook_secret or "").strip()
    if not webhook_secret:
        # We refuse rather than 200-with-noop so Stripe surfaces a
        # broken destination in the dashboard immediately. A silent
        # 200 would hide a mis-provisioned env and cause events to
        # accumulate as "delivered but ignored".
        logger.warning("stripe_webhook.event.no_signing_secret_set")
        return JSONResponse({"ok": False}, status_code=503)

    try:
        import stripe  # local import so a missing SDK never blocks app boot
    except ImportError:
        logger.warning("stripe_webhook.event.sdk_unavailable")
        return JSONResponse({"ok": False}, status_code=503)

    raw_body = await request.body()
    header_sig = request.headers.get("stripe-signature") or ""
    logger.info(
        "stripe_webhook.event.received body_len=%s signature_present=%s",
        len(raw_body),
        bool(header_sig),
    )

    if not header_sig:
        logger.warning("stripe_webhook.event.missing_signature len=%s", len(raw_body))
        return JSONResponse({"ok": False}, status_code=400)

    try:
        event = stripe.Webhook.construct_event(
            payload=raw_body,
            sig_header=header_sig,
            secret=webhook_secret,
        )
    except ValueError:
        # ValueError is Stripe's documented "invalid payload" signal
        # (e.g. malformed JSON, truncated body). We surface a 400 so
        # their delivery view flags the event and stops retrying past
        # their normal budget.
        logger.warning("stripe_webhook.event.bad_payload len=%s", len(raw_body))
        return JSONResponse({"ok": False}, status_code=400)
    except stripe.SignatureVerificationError:
        # Path chosen deliberately over ``stripe.error.SignatureVerificationError``:
        # both refer to the same class in stripe 12.x, but the top-level
        # attribute is the one the stubs surface for type checkers.
        logger.warning("stripe_webhook.event.bad_signature len=%s", len(raw_body))
        return JSONResponse({"ok": False}, status_code=400)
    except Exception:
        # Belt against a future SDK exception we don't know about.
        # Still refuse rather than 200 — a false success would silence
        # a real problem.
        logger.exception("stripe_webhook.event.verify_raised")
        return JSONResponse({"ok": False}, status_code=400)

    event_id, event_type = _stripe_event_meta(event)
    # Only the id + type. Never the payload. This is deliberate: card
    # PANs, bank details, payout amounts, customer emails, and any
    # personal data ride inside `event.data` and are not to be logged
    # by this receiving-foundation.
    logger.info(
        "stripe_webhook.event.verified event_id=%s event_type=%s",
        event_id,
        event_type,
    )

    # Business handlers slot in later. For now, every verified event
    # is acknowledged 200 so Stripe stops retrying and marks the
    # delivery successful in the dashboard.
    return JSONResponse(
        {"received": True, "event_id": event_id}, status_code=200
    )


def _stripe_event_meta(event: Any) -> tuple[str, str]:
    """Return (event_id, event_type) as strings.

    Stripe's SDK returns either a ``stripe.Event`` object or, when
    called through the raw payload path, a dict-like. Both expose
    ``id`` + ``type`` via attribute AND item access. Read both defensively
    so a future SDK signature change here can't tank the ack.
    """
    def _read(obj: Any, key: str) -> str:
        try:
            value = obj[key]
        except (KeyError, TypeError):
            value = getattr(obj, key, "")
        return str(value or "")

    return _read(event, "id"), _read(event, "type")
