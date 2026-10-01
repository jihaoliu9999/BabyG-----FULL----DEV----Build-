"""Lazy, server-side entry point for outbound Stripe API access."""

import stripe

from app.config import get_settings


def get_stripe_client() -> stripe.StripeClient:
    key = get_settings().stripe_secret_key.strip()
    if not key:
        raise RuntimeError("Stripe is not configured")
    try:
        return stripe.StripeClient(api_key=key)
    except Exception:
        raise RuntimeError("Stripe client initialization failed") from None
