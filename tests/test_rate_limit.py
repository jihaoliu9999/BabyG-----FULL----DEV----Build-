"""Tests for the in-process token-bucket rate limiter.

Locks in:
  * 5-per-IP cap on `/auth/magic-link` (the only abuse-prevention
    surface on an unauth endpoint)
  * `client_ip` reads the LAST entry of `X-Forwarded-For`, not the first
    (the first is attacker-controlled — the proxy *appends*, not
    replaces)
  * Empty/missing keys are not rate-limited (refusing-to-key would deny
    everyone the moment a header parser hiccups)
  * Bucket cardinality is bounded so the limiter can't be turned into a
    memory exhaustion sink
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.core import rate_limit as rate_limit_module
from app.core.rate_limit import RateLimiter, client_ip


def _fake_request(*, xff: str | None = None, peer: str | None = "127.0.0.1") -> Any:
    headers = {}
    if xff is not None:
        headers["x-forwarded-for"] = xff
    return SimpleNamespace(
        headers=headers,
        client=SimpleNamespace(host=peer) if peer else None,
    )


# ---------- client_ip ----------


def test_client_ip_uses_last_xff_entry():
    """Last entry is the one the trusted proxy saw; the first is whatever
    the attacker dropped into the request."""
    ip = client_ip(_fake_request(xff="1.2.3.4, 10.0.0.1, 172.16.0.5"))
    assert ip == "172.16.0.5"


def test_client_ip_strips_whitespace():
    assert client_ip(_fake_request(xff="1.2.3.4,   10.0.0.1   ")) == "10.0.0.1"


def test_client_ip_single_xff_entry():
    assert client_ip(_fake_request(xff="203.0.113.7")) == "203.0.113.7"


def test_client_ip_falls_back_to_peer_when_no_xff():
    assert client_ip(_fake_request(peer="198.51.100.9")) == "198.51.100.9"


def test_client_ip_empty_xff_falls_back_to_peer():
    assert client_ip(_fake_request(xff="", peer="198.51.100.9")) == "198.51.100.9"


def test_client_ip_xff_with_only_commas_falls_back_to_peer():
    assert client_ip(_fake_request(xff=",,", peer="198.51.100.9")) == "198.51.100.9"


def test_client_ip_no_peer_returns_empty_string():
    assert client_ip(_fake_request(peer=None)) == ""


def test_client_ip_xff_attacker_spoof_does_not_win():
    """If the attacker writes 1.1.1.1 and the proxy appends the real
    client, we MUST key off the real client. This is the regression
    test for the X-F-F bypass that batch 7 closed."""
    ip = client_ip(_fake_request(xff="1.1.1.1, 9.9.9.9"))
    assert ip == "9.9.9.9"
    assert ip != "1.1.1.1"


# ---------- RateLimiter ----------


def test_rate_limiter_allows_first_capacity_requests():
    rl = RateLimiter(capacity=3, refill_per_second=0.0)
    for _ in range(3):
        assert rl.allow("scope", "k") is True
    assert rl.allow("scope", "k") is False


def test_rate_limiter_keys_are_independent():
    rl = RateLimiter(capacity=1, refill_per_second=0.0)
    assert rl.allow("scope", "a") is True
    assert rl.allow("scope", "b") is True
    assert rl.allow("scope", "a") is False


def test_rate_limiter_scopes_are_independent():
    rl = RateLimiter(capacity=1, refill_per_second=0.0)
    assert rl.allow("magic-link", "k") is True
    assert rl.allow("other", "k") is True
    assert rl.allow("magic-link", "k") is False


def test_rate_limiter_empty_key_short_circuits():
    """No key means we can't rate limit; deny would block everyone in a
    parsing edge case, so we allow."""
    rl = RateLimiter(capacity=0, refill_per_second=0.0)
    assert rl.allow("scope", "") is True


def test_rate_limiter_refills_over_time(monkeypatch):
    """Token refills happen on the next request based on monotonic time.
    We mock time.monotonic to keep the test deterministic."""
    clock = {"t": 1000.0}

    def _now() -> float:
        return clock["t"]

    monkeypatch.setattr(rate_limit_module.time, "monotonic", _now)

    rl = RateLimiter(capacity=2, refill_per_second=1.0)  # 1 token / s
    assert rl.allow("scope", "k") is True   # 1
    assert rl.allow("scope", "k") is True   # 0
    assert rl.allow("scope", "k") is False  # still 0

    clock["t"] += 1.0  # advance one second -> 1 token refilled
    assert rl.allow("scope", "k") is True
    assert rl.allow("scope", "k") is False


def test_rate_limiter_caps_bucket_cardinality():
    """Each new key allocates a bucket; with `max_keys=4` the 5th key
    must evict the oldest. Otherwise an attacker rotating IPs could
    grow the dict without bound."""
    rl = RateLimiter(capacity=1, refill_per_second=0.0, max_keys=4)
    for k in ("a", "b", "c", "d", "e"):
        rl.allow("scope", k)
    assert len(rl._buckets) == 4
    # `a` was inserted first, no subsequent access -> evicted.
    assert ("scope", "a") not in rl._buckets
    assert ("scope", "e") in rl._buckets


def test_magic_link_limiter_default_settings():
    """Lock the 5/10-min cap on the production-installed limiter so a
    silent retune is caught in review."""
    assert rate_limit_module.magic_link_limiter.capacity == 5.0
    # 1 token every 120s = 5 per 600s.
    assert rate_limit_module.magic_link_limiter.refill == pytest.approx(1 / 120.0)


# ---------- end-to-end through /auth/magic-link ----------


def test_magic_link_route_returns_friendly_error_when_limited(monkeypatch):
    """The route must surface the cap as a login-page error, not a 429
    JSON; otherwise users hit a raw error and don't know to wait."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from app.core import supabase_client as sb
    from app.main import app
    from app.routes import auth as auth_route

    # Replace the production limiter with a low-cap one for fast
    # assertion. The route reads `auth_route.magic_link_limiter` (bound
    # at import time), so we patch the module attribute directly.
    fast_limiter = RateLimiter(capacity=1, refill_per_second=0.0)
    monkeypatch.setattr(auth_route, "magic_link_limiter", fast_limiter)

    # Stub the Supabase anon client so sign_in_with_otp doesn't reach
    # the network. The operator-email gate is bypassed directly so we
    # don't need to fake a chainable service client just for that path.
    class _StubAuth:
        def sign_in_with_otp(self, args):
            return SimpleNamespace()

    monkeypatch.setattr(
        sb, "get_anon_client", lambda: SimpleNamespace(auth=_StubAuth())
    )
    monkeypatch.setattr(auth_route, "_operator_email_authorized", lambda _e: False)

    client = TestClient(app, follow_redirects=False)
    payload = {"email": "alice@example.com", "role": "creator"}

    r1 = client.post("/auth/magic-link", data=payload)
    assert r1.status_code == 303
    assert r1.headers["location"] == "/auth/magic-link/sent?role=creator"

    # Second send is rate-limited. The route renders an HTML login page
    # with status 400 (not a 429 JSON), so users get a recoverable error.
    r2 = client.post("/auth/magic-link", data=payload)
    assert r2.status_code == 400
    assert "Too many sign-in attempts" in r2.text
