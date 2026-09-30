"""POST /webhooks/stripe — signature verification + safe-ack contract.

Covers the receiving-foundation guarantees:

  * valid Stripe signature      → 200 with {"received": true, ...}
  * missing Stripe-Signature    → 400
  * bad Stripe-Signature        → 400
  * malformed payload           → 400
  * valid but unknown event     → 200 (Stripe won't retry forever)
  * no DB write / no business   action fires on a verified event
  * secret unconfigured         → 503

The valid-signature tests build the ``Stripe-Signature`` header exactly
the way Stripe does: ``t=<ts>,v1=<hmac_sha256(secret, "<ts>.<payload>")>``.
That's the same string ``stripe.Webhook.construct_event`` verifies, so
the assertion path exercises the real Stripe SDK code — not a stub.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import supabase_client
from app.main import app

TEST_SECRET = "whsec_test_babyg_settings_override"


def _sign(payload: bytes, *, secret: str = TEST_SECRET, timestamp: int | None = None) -> str:
    """Build a valid ``Stripe-Signature`` header for ``payload``."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.{payload.decode('utf-8')}".encode()
    mac = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def _stripe_event(event_type: str, event_id: str = "evt_test_babyg_123") -> bytes:
    body = {
        "id": event_id,
        "object": "event",
        "type": event_type,
        "api_version": "2024-06-20",
        "created": int(time.time()),
        "livemode": False,
        "data": {"object": {"id": "obj_test_babyg_123"}},
    }
    return json.dumps(body).encode("utf-8")


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _stripe_secret(monkeypatch: pytest.MonkeyPatch):
    """Point the singleton settings at a test-only signing secret."""
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", TEST_SECRET)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# ---------- signature verification contract ----------


def test_valid_signature_returns_200_with_event_id(client: TestClient) -> None:
    body = _stripe_event("payment_intent.succeeded")
    sig = _sign(body)
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"received": True, "event_id": "evt_test_babyg_123"}


def test_invalid_signature_is_rejected(client: TestClient) -> None:
    body = _stripe_event("payment_intent.succeeded")
    bad_sig = _sign(body, secret="whsec_wrong_key")
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": bad_sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json() == {"ok": False}


def test_missing_signature_header_is_rejected(client: TestClient) -> None:
    body = _stripe_event("payment_intent.succeeded")
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert r.status_code == 400
    assert r.json() == {"ok": False}


def test_malformed_payload_is_rejected(client: TestClient) -> None:
    """A body that isn't valid JSON must be rejected — the SDK's
    ``construct_event`` raises ValueError before verifying, and our
    route surfaces that as a 400."""
    body = b"this-is-not-json"
    sig = _sign(body)
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 400


def test_valid_unknown_event_type_is_safely_acknowledged(client: TestClient) -> None:
    """Stripe emits event types we don't handle yet (any type outside
    our future dispatcher). The receiving foundation must still return
    2xx so Stripe stops retrying them."""
    body = _stripe_event("customer.subscription.paused", event_id="evt_unknown_1")
    sig = _sign(body)
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json()["received"] is True
    assert r.json()["event_id"] == "evt_unknown_1"


# ---------- no side effects on verified events ----------


def test_valid_verified_event_does_not_touch_supabase(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The receiving foundation MUST NOT run any business logic. Prove
    it by asserting the shared service-role Supabase client is never
    instantiated during a verified event's request cycle."""
    calls: list[str] = []

    def _fail_on_call(*args, **kwargs):
        calls.append("get_service_client")
        raise AssertionError(
            "Stripe webhook receiver invoked Supabase — no DB writes allowed yet."
        )

    monkeypatch.setattr(supabase_client, "get_service_client", _fail_on_call)

    body = _stripe_event("charge.succeeded", event_id="evt_no_db_side_effect")
    sig = _sign(body)
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert calls == []


def test_verified_event_log_omits_payload(
    client: TestClient, caplog: pytest.LogCaptureFixture
) -> None:
    """Only event id + type may be logged — never card / customer /
    bank data. We embed a sentinel token inside the event body and
    assert it does not surface in log records."""
    sentinel = "SUPER-SENSITIVE-PAN-4242424242424242"
    body = json.dumps(
        {
            "id": "evt_log_safety_1",
            "object": "event",
            "type": "payment_intent.succeeded",
            "api_version": "2024-06-20",
            "created": int(time.time()),
            "livemode": False,
            "data": {"object": {"id": "pi_test", "secret_note": sentinel}},
        }
    ).encode("utf-8")
    sig = _sign(body)
    with caplog.at_level("DEBUG"):
        r = client.post(
            "/webhooks/stripe",
            content=body,
            headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
        )
    assert r.status_code == 200
    for record in caplog.records:
        assert sentinel not in record.getMessage()


# ---------- unconfigured-secret behavior ----------


def test_missing_signing_secret_returns_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("STRIPE_WEBHOOK_SECRET", "")
    get_settings.cache_clear()
    body = _stripe_event("payment_intent.succeeded")
    sig = _sign(body)
    r = client.post(
        "/webhooks/stripe",
        content=body,
        headers={"Stripe-Signature": sig, "Content-Type": "application/json"},
    )
    # 503 rather than 200 so Stripe surfaces the mis-provisioned
    # destination immediately — see the route's docstring.
    assert r.status_code == 503
    assert r.json() == {"ok": False}


# ---------- Instagram receiver stays isolated ----------


def test_instagram_webhook_route_still_present(client: TestClient) -> None:
    """Prove the Stripe addition didn't move or shadow the existing
    Meta receiver. A GET on the Instagram verify path must still be
    reachable — its status is dictated by Meta's env, not by any
    Stripe change. Any 4xx status proves the route resolved (a
    404 would mean the receiver was shadowed by the new route)."""
    r = client.get("/webhooks/instagram", params={"hub.mode": "subscribe"})
    assert r.status_code != 404, "Instagram receiver was shadowed by the Stripe change"
    assert 400 <= r.status_code < 500
