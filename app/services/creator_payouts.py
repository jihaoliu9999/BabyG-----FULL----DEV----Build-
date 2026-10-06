"""Creator-owned Stripe Connect onboarding; no payments or transfers."""

from __future__ import annotations

import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from app.config import get_settings
from app.core import supabase_client
from app.services.stripe_client import get_stripe_client, key_kind, safe_error_fields

logger = logging.getLogger(__name__)

TABLE = "creator_payout_accounts"

# Stripe saves the first result for an idempotency key -- errors included --
# for 24 hours once the endpoint has started executing. A key that never
# changes per creator would therefore replay a failed account create (for
# example "Connect is not set up on the platform yet") for a full day after
# the cause is fixed. The key still collapses a double-submitted Start, which
# lands within the same short window.
IDEMPOTENCY_WINDOW_SECONDS = 60

# Accounts v1 with explicit controller properties. Stripe no longer accepts
# `type` for this platform's new connected accounts and requires the fee
# payer and loss liability to be stated at creation. These values are the
# Express configuration `type=express` stood for: Stripe-hosted onboarding
# (Account Links) and requirement collection, the Express Dashboard, and
# babyg paying Stripe fees and carrying negative-balance liability.
ACCOUNT_CONTROLLER: dict[str, Any] = {
    "fees": {"payer": "application"},
    "losses": {"payments": "application"},
    "requirement_collection": "stripe",
    "stripe_dashboard": {"type": "express"},
}

_EMAIL = re.compile(r"[^\s@'\"]+@[^\s@'\"]+")


class PayoutSetupError(Exception):
    """A payout setup step could not be completed safely."""


def _log_failure(stage: str, exc: BaseException | None = None, **context: Any) -> None:
    """One WARNING per failed step, naming the step and Stripe's reason.
    Never logs the key, request parameters or response bodies."""
    fields: dict[str, Any] = {"stage": stage, "key_kind": key_kind(), **context}
    if exc is not None:
        fields.update(safe_error_fields(exc))
        # Stripe may echo the account email back in a validation message.
        fields["message"] = _EMAIL.sub("[email]", str(fields.get("message") or ""))
    logger.warning(
        "creator_payouts.failed %s",
        " ".join(f"{name}={value!r}" for name, value in fields.items()),
    )


def _account_email(creator_user_id: str) -> str:
    """The creator's babyg login email (public.users.email), which Stripe
    requires at creation for an account that only receives transfers."""
    result = (
        supabase_client.get_service_client()
        .table("users")
        .select("email")
        .eq("id", creator_user_id)
        .limit(1)
        .execute()
    )
    rows = result.data or []
    email = str(rows[0].get("email") or "").strip() if rows else ""
    if "@" not in email:
        raise PayoutSetupError("Payout setup is unavailable")
    return email


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


def readiness(creator_user_id: str) -> tuple[str, str | None]:
    """Readiness from Stripe (never from an onboarding redirect), plus the
    mapped account id."""
    stage = "status_mapping_read"
    try:
        account_id = _mapping(creator_user_id)
        if account_id is None:
            return "not_set_up", None
        stage = "status_account_retrieve"
        account = _client().v1.accounts.retrieve(account_id)
        capabilities = account.get("capabilities") or {}
        if account.get("payouts_enabled") and capabilities.get("transfers") == "active":
            return "ready", account_id
        return "incomplete", account_id
    except Exception as exc:
        _log_failure(stage, exc)
        return "unavailable", None


def payout_status(creator_user_id: str) -> str:
    """Read readiness from Stripe, not from an onboarding redirect."""
    return readiness(creator_user_id)[0]


def ready_account_id(creator_user_id: str) -> str | None:
    """The creator's connected account, only when Stripe reports it ready
    (payouts enabled and the transfers capability active)."""
    status, account_id = readiness(creator_user_id)
    return account_id if status == "ready" else None


def _public_origin() -> str:
    settings = get_settings()
    origin = (settings.public_app_url or settings.app_url).rstrip("/")
    parsed = urlsplit(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        _log_failure("origin", origin_scheme=parsed.scheme, origin_has_host=bool(parsed.netloc))
        raise PayoutSetupError("Payout setup is unavailable")
    if settings.is_production and parsed.scheme != "https":
        _log_failure("origin", origin_scheme=parsed.scheme, production=True)
        raise PayoutSetupError("Payout setup is unavailable")
    return origin


def _client():
    # This feature is intentionally sandbox-only until a later production
    # payment rollout explicitly authorizes live Stripe operations.
    if not get_settings().stripe_secret_key.strip().startswith("sk_test_"):
        raise PayoutSetupError("Payout setup is unavailable")
    return get_stripe_client()


def _idempotency_key(creator_user_id: str) -> str:
    window = int(time.time() // IDEMPOTENCY_WINDOW_SECONDS)
    return f"babyg-creator-connect-{window}-{creator_user_id}"


def onboarding_url(creator_user_id: str, *, create_if_missing: bool) -> str:
    """Create/reuse the creator's Express account and a one-use hosted link."""
    stage = "config"
    try:
        client = _client()
        stage = "origin"
        origin = _public_origin()
        stage = "mapping_read"
        account_id = _mapping(creator_user_id)
        if account_id is None:
            if not create_if_missing:
                stage = "refresh_without_account"
                raise PayoutSetupError("Payout setup is unavailable")
            stage = "account_email"
            email = _account_email(creator_user_id)
            stage = "account_create"
            account = client.v1.accounts.create(
                {
                    "controller": ACCOUNT_CONTROLLER,
                    "email": email,
                    "capabilities": {"transfers": {"requested": True}},
                },
                options={"idempotency_key": _idempotency_key(creator_user_id)},
            )
            created_id = account.get("id")
            if not isinstance(created_id, str) or not created_id.startswith("acct_"):
                stage = "account_create_response"
                raise PayoutSetupError("Payout setup is unavailable")
            stage = "mapping_insert"
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
        stage = "account_link"
        link = client.v1.account_links.create(
            {
                "account": account_id,
                "type": "account_onboarding",
                "return_url": f"{origin}/creator/payouts/return",
                "refresh_url": f"{origin}/creator/payouts/refresh",
            }
        )
        stage = "account_link_url"
        url = link.get("url")
        parsed = urlsplit(url) if isinstance(url, str) else None
        if not parsed or parsed.scheme != "https" or parsed.hostname != "connect.stripe.com":
            raise PayoutSetupError("Payout setup is unavailable")
        return url
    except Exception as exc:
        if stage != "origin":  # _public_origin logged its own detail
            _log_failure(stage, exc)
        if isinstance(exc, PayoutSetupError):
            raise
        raise PayoutSetupError("Payout setup is unavailable") from None
