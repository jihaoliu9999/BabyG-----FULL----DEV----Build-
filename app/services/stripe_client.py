"""Lazy, server-side entry point for outbound Stripe API access."""

import re
from typing import Any

import stripe

from app.config import get_settings

# Anything shaped like a Stripe credential is masked before a message is
# logged. Stripe already masks keys in its own errors; this is a backstop.
_CREDENTIAL = re.compile(r"\b(?:sk|rk|pk|whsec)_[A-Za-z0-9_*]+")
_MESSAGE_MAX = 1000  # Stripe validation messages can run past 300 chars


def get_stripe_client() -> stripe.StripeClient:
    key = get_settings().stripe_secret_key.strip()
    if not key:
        raise RuntimeError("Stripe is not configured")
    try:
        return stripe.StripeClient(api_key=key)
    except Exception:
        raise RuntimeError("Stripe client initialization failed") from None


def key_kind() -> str:
    """Which KIND of key is configured (``sk_test``, ``sk_live``, ...).
    Never any part of the key itself."""
    key = (get_settings().stripe_secret_key or "").strip()
    if not key:
        return "missing"
    for prefix in ("sk_test_", "sk_live_", "rk_test_", "rk_live_"):
        if key.startswith(prefix):
            return prefix[:-1]
    return "unrecognized"


def safe_error_fields(exc: BaseException) -> dict[str, Any]:
    """Loggable facts about a failed Stripe (or other) call: the class, and
    Stripe's own code / type / HTTP status / request id / message. No
    request parameters and no credentials."""
    error = getattr(exc, "error", None)
    message = getattr(exc, "user_message", None) or str(exc)
    return {
        "error": type(exc).__name__,
        "http_status": getattr(exc, "http_status", None),
        "stripe_code": getattr(exc, "code", None),
        "stripe_type": getattr(error, "type", None),
        "request_id": getattr(exc, "request_id", None),
        "message": _CREDENTIAL.sub("[redacted]", str(message))[:_MESSAGE_MAX],
    }
