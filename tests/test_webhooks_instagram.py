"""Tests for the /webhooks/instagram endpoint.

The two things worth locking in with tests:
1. Verify challenge only echoes when the token matches — a wrong or
   missing token must always 403 (this is the door for Meta to
   subscribe our webhook; if it's loose, anyone who guesses the
   verify string can hijack subscription setup).
2. Event POST signature verification is HMAC-SHA256 with the app
   secret, constant-time compared. Any tampering or missing header
   must 403; a valid signature must 200. Ingestion errors past the
   signature must not leak as 5xx (Meta retries on 5xx, and a
   retry storm on a real bug is worse than dropping the payload).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.csrf import CSRF_EXEMPT_PATHS
from app.routes import webhooks


@pytest.fixture
def client(monkeypatch):
    """A minimal FastAPI app with only the webhooks router mounted.
    Avoids booting the whole babyg app + its CSRF/session middleware
    for a route-level test."""
    from app.config import get_settings

    # Wipe the settings cache so per-test env changes take effect.
    get_settings.cache_clear()
    app = FastAPI()
    app.include_router(webhooks.router)
    return TestClient(app)


def _stub_settings(monkeypatch, verify_token: str = "", app_secret: str = ""):
    """Replace get_settings() with a stub carrying just what the route reads."""
    class _Stub:
        pass

    stub = _Stub()
    stub.instagram_webhook_verify_token = verify_token
    stub.instagram_app_secret = app_secret
    monkeypatch.setattr(webhooks, "get_settings", lambda: stub)


# ---- verify challenge (GET) -----------------------------------------


def test_verify_403_when_env_token_empty(client, monkeypatch) -> None:
    _stub_settings(monkeypatch, verify_token="")
    r = client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "anything",
            "hub.challenge": "1234",
        },
    )
    assert r.status_code == 403


def test_verify_403_on_token_mismatch(client, monkeypatch) -> None:
    _stub_settings(monkeypatch, verify_token="secret-token")
    r = client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "wrong",
            "hub.challenge": "1234",
        },
    )
    assert r.status_code == 403


def test_verify_403_when_mode_not_subscribe(client, monkeypatch) -> None:
    _stub_settings(monkeypatch, verify_token="secret-token")
    r = client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "delete",
            "hub.verify_token": "secret-token",
            "hub.challenge": "1234",
        },
    )
    assert r.status_code == 403


def test_verify_200_echoes_challenge_on_match(client, monkeypatch) -> None:
    _stub_settings(monkeypatch, verify_token="secret-token")
    r = client.get(
        "/webhooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "secret-token",
            "hub.challenge": "1234567890",
        },
    )
    assert r.status_code == 200
    # Must be raw text — Meta refuses JSON here.
    assert r.text == "1234567890"


# ---- event POST + HMAC ---------------------------------------------


def _sign(app_secret: str, raw: bytes) -> str:
    return "sha256=" + hmac.new(
        app_secret.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()


def test_event_403_when_app_secret_empty(client, monkeypatch) -> None:
    _stub_settings(monkeypatch, app_secret="")
    r = client.post(
        "/webhooks/instagram",
        content=b"{}",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 403


def test_event_403_when_signature_missing(client, monkeypatch) -> None:
    _stub_settings(monkeypatch, app_secret="app-secret-hex")
    r = client.post(
        "/webhooks/instagram",
        content=b'{"entry":[]}',
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 403


def test_event_403_when_signature_wrong(client, monkeypatch, caplog) -> None:
    _stub_settings(monkeypatch, app_secret="app-secret-hex")
    with caplog.at_level(logging.WARNING):
        r = client.post(
            "/webhooks/instagram",
            content=b'{"entry":[]}',
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": "sha256=" + "0" * 64,
            },
        )
    assert r.status_code == 403
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "instagram_webhook.event.bad_signature" in log_text


def test_event_200_on_valid_signature(client, monkeypatch, caplog) -> None:
    secret = "app-secret-hex"
    body = json.dumps({"object": "instagram", "entry": []}).encode("utf-8")
    _stub_settings(monkeypatch, app_secret=secret)

    dispatched: list = []
    monkeypatch.setattr(
        webhooks, "_dispatch_payload", lambda p: dispatched.append(p)
    )

    with caplog.at_level(logging.INFO):
        r = client.post(
            "/webhooks/instagram",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": _sign(secret, body),
            },
        )
    assert r.status_code == 200
    assert dispatched == [{"object": "instagram", "entry": []}]
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "instagram_webhook.event.received" in log_text
    assert "instagram_webhook.event.parsed object=instagram entries=0" in log_text


def test_event_200_when_json_is_broken_but_signature_ok(
    client, monkeypatch
) -> None:
    """Meta retries on 5xx. Even a signed-but-malformed payload should
    ack — we can't do anything with it and don't want retry storms."""
    secret = "app-secret-hex"
    body = b"not-json"
    _stub_settings(monkeypatch, app_secret=secret)
    monkeypatch.setattr(webhooks, "_dispatch_payload", lambda p: None)
    r = client.post(
        "/webhooks/instagram",
        content=body,
        headers={
            "content-type": "application/json",
            "x-hub-signature-256": _sign(secret, body),
        },
    )
    assert r.status_code == 200


def test_event_200_when_dispatch_raises(client, monkeypatch) -> None:
    """Same reason — if the ingestion service raises past our
    top-level try/except, we still ack Meta and drop the payload."""
    secret = "app-secret-hex"
    body = b'{"object":"instagram","entry":[]}'
    _stub_settings(monkeypatch, app_secret=secret)

    def _boom(_):
        raise RuntimeError("supabase down mid-ingest")

    monkeypatch.setattr(webhooks, "_dispatch_payload", _boom)
    # dispatch_payload internally has its own try/except, so the raise
    # never surfaces — the outer route still returns 200.
    # We test this by NOT overriding the outer route's ack.
    # If _dispatch_payload's own except was missing, this test would
    # 500. This locks in that the route path is protected.
    # (The webhook implementation catches inside _dispatch_payload
    # itself, so raising here would still crash — but the design
    # says _dispatch_payload never raises, so we assert that contract
    # by wrapping the test in a try to catch the leak.)
    # Because the test above verifies dispatch_payload is called, and
    # _dispatch_payload in production code has try/except, this test
    # documents the contract rather than exercising a real failure.
    try:
        r = client.post(
            "/webhooks/instagram",
            content=body,
            headers={
                "content-type": "application/json",
                "x-hub-signature-256": _sign(secret, body),
            },
        )
        assert r.status_code == 200
    except RuntimeError:
        pytest.fail(
            "webhook route leaked a dispatch exception — _dispatch_payload "
            "must swallow all inner failures"
        )


# ---- CSRF exemption -----------------------------------------------


def test_webhook_path_is_csrf_exempt() -> None:
    """Meta doesn't know our session. If the CSRF middleware isn't
    told to skip this path, every POST 403s before the signature
    check ever runs."""
    assert "/webhooks/instagram" in CSRF_EXEMPT_PATHS
