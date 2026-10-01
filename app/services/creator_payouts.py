"""Creator-owned Stripe Connect onboarding; no payments or transfers."""

from __future__ import annotations

from urllib.parse import urlsplit

from app.config import get_settings
from app.core import supabase_client
from app.services.stripe_client import get_stripe_client

TABLE = "creator_payout_accounts"


class PayoutSetupError(Exception):
    """A payout setup step could not be completed safely."""


def _mapping(creator_user_id: str) -> str | None:
    result = (
        supabase_client.get_service_client()
        .table(TABLE)
        .select("stripe_account_id")
        .eq("creator_user_id", creator_user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    return str(rows[0]["stripe_account_id"]) if rows else None


def payout_status(creator_user_id: str) -> str:
    """Read readiness from Stripe, not from an onboarding redirect."""
    try:
        account_id = _mapping(creator_user_id)
        if account_id is None:
            return "not_set_up"
        account = _client().v1.accounts.retrieve(account_id)
        capabilities = account.get("capabilities") or {}
        if account.get("payouts_enabled") and capabilities.get("transfers") == "active":
            return "ready"
        return "incomplete"
    except Exception:
        return "unavailable"


def _public_origin() -> str:
    settings = get_settings()
    origin = (settings.public_app_url or settings.app_url).rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise PayoutSetupError("Payout setup is unavailable")
    if settings.is_production and parsed.scheme != "https":
        raise PayoutSetupError("Payout setup is unavailable")
    return origin


def _client():
    # This feature is intentionally sandbox-only until a later production
    # payment rollout explicitly authorizes live Stripe operations.
    if not get_settings().stripe_secret_key.strip().startswith("sk_test_"):
        raise PayoutSetupError("Payout setup is unavailable")
    return get_stripe_client()


def onboarding_url(creator_user_id: str, *, create_if_missing: bool) -> str:
    """Create/reuse the creator's Express account and a one-use hosted link."""
    try:
        client = _client()
        origin = _public_origin()
        account_id = _mapping(creator_user_id)
        if account_id is None:
            if not create_if_missing:
                raise PayoutSetupError("Payout setup is unavailable")
            account = client.v1.accounts.create(
                {"type": "express", "capabilities": {"transfers": {"requested": True}}},
                options={"idempotency_key": f"babyg-creator-connect-{creator_user_id}"},
            )
            created_id = account.get("id")
            if not isinstance(created_id, str) or not created_id.startswith("acct_"):
                raise PayoutSetupError("Payout setup is unavailable")
            try:
                supabase_client.get_service_client().table(TABLE).insert(
                    {"creator_user_id": creator_user_id, "stripe_account_id": created_id}
                ).execute()
                account_id = created_id
            except Exception:
                # A concurrent start may have saved the same creator's mapping.
                # Never use the new account if a different mapping won the race.
                account_id = _mapping(creator_user_id)
                if account_id is None:
                    raise
        link = client.v1.account_links.create(
            {
                "account": account_id,
                "type": "account_onboarding",
                "return_url": f"{origin}/creator/payouts/return",
                "refresh_url": f"{origin}/creator/payouts/refresh",
            }
        )
        url = link.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or parsed.hostname != "connect.stripe.com":
            raise PayoutSetupError("Payout setup is unavailable")
        return url
    except Exception as exc:
        if isinstance(exc, PayoutSetupError):
            raise
        raise PayoutSetupError("Payout setup is unavailable") from None
