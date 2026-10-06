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

# New connected accounts are created with Accounts v2 (POST /v2/core/accounts):
# Stripe rejects Accounts v1 creation for this platform ("Create connected
# accounts with POST /v2/core/accounts instead"). The installed SDK
# (stripe 12.5.1, pinned to 2025-08-27.basil for every v1 call Step 7A makes)
# has no typed v2 Accounts service, so the v2 calls go through the SDK's own
# raw_request with this explicit v2 API version. The request shape below is
# the 2026-09-30.endive contract (Stripe's published OpenAPI spec and SDK).
ACCOUNTS_V2_API_VERSION = "2026-09-30.endive"

# Creator payouts are US-only: every offer, deal and payment is USD-only at
# the schema level (0049/0051/0052). Stripe requires identity.country before a
# recipient configuration can be set (identity_country_required); the value is
# an ISO 3166-1 alpha-2 code, lowercase as in Stripe's v2 examples.
PAYOUT_COUNTRY = "us"

# The creator is never the merchant of record: babyg charges the payer and
# routes funds with a destination charge, which is exactly Stripe's
# "recipient" configuration. Stripe-hosted onboarding and the Express
# Dashboard as before; with an Express Dashboard Stripe requires babyg to be
# the fee and loss collector (account_controller_express_dash_without_
# application_losses_or_fees), which is what type=express meant.
def _new_account_request(email: str) -> dict[str, Any]:
    return {
        "contact_email": email,
        "identity": {"country": PAYOUT_COUNTRY},
        "dashboard": "express",
        "defaults": {
            "responsibilities": {"fees_collector": "application", "losses_collector": "application"}
        },
        "configuration": {
            "recipient": {"capabilities": {"stripe_balance": {"stripe_transfers": {"requested": True}}}}
        },
    }


# Stripe's documented answer when a v1-created account is read through v2.
_V1_ACCOUNT_CODES = frozenset({"v1_account_instead_of_v2_account", "account_not_yet_compatible_with_v2"})
_ACCOUNT_ID = re.compile(r"acct_[A-Za-z0-9]+")

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


def _v2(client: Any, method: str, path: str, params: dict[str, Any] | None = None,
        *, idempotency_key: str | None = None) -> dict[str, Any]:
    options: dict[str, Any] = {"stripe_version": ACCOUNTS_V2_API_VERSION}
    if idempotency_key:
        options["idempotency_key"] = idempotency_key
    response = client.raw_request(method, path, **(params or {}), **options)
    data = getattr(response, "data", None)
    if not isinstance(data, dict):
        raise ValueError("unexpected Stripe v2 response")
    return data


def _v2_status(client: Any, account_id: str) -> str | None:
    """'ready' / 'incomplete' for an Accounts v2 account, or None when Stripe
    says the id is a v1-created account (read those through v1)."""
    if not _ACCOUNT_ID.fullmatch(account_id):
        raise ValueError("unexpected stored account id")
    try:
        account = _v2(client, "get", f"/v2/core/accounts/{account_id}",
                      {"include[0]": "configuration.recipient"})
    except Exception as exc:
        if getattr(exc, "code", None) in _V1_ACCOUNT_CODES:
            return None
        raise
    recipient = (account.get("configuration") or {}).get("recipient") or {}
    balance = (recipient.get("capabilities") or {}).get("stripe_balance") or {}
    transfers = (balance.get("stripe_transfers") or {}).get("status")
    payouts = balance.get("payouts")
    # The v1 rule was "payouts enabled and transfers active". Stripe reports
    # the recipient payouts capability on its own; when it is present it must
    # be active too.
    payouts_ok = payouts is None or (payouts or {}).get("status") == "active"
    if recipient.get("applied") and transfers == "active" and payouts_ok:
        return "ready"
    return "incomplete"


def readiness(creator_user_id: str) -> tuple[str, str | None]:
    """Readiness from Stripe (never from an onboarding redirect), plus the
    mapped account id."""
    stage = "status_mapping_read"
    try:
        account_id = _mapping(creator_user_id)
        if account_id is None:
            return "not_set_up", None
        stage = "status_account_retrieve"
        client = _client()
        status = _v2_status(client, account_id)
        if status is not None:
            return status, account_id
        # An account created through Accounts v1 (before the v2 switch).
        account = client.v1.accounts.retrieve(account_id)
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
    return f"babyg-creator-account-v2-{window}-{creator_user_id}"


def _stripe_hosted(url: Any) -> bool:
    """A Stripe-hosted https page (Account Links); never an arbitrary host."""
    if not isinstance(url, str):
        return False
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme == "https" and (host == "stripe.com" or host.endswith(".stripe.com"))


def onboarding_url(creator_user_id: str, *, create_if_missing: bool) -> str:
    """Create/reuse the creator's connected account (Accounts v2 recipient,
    Express Dashboard) and a one-use Stripe-hosted onboarding link."""
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
            account = _v2(client, "post", "/v2/core/accounts", _new_account_request(email),
                          idempotency_key=_idempotency_key(creator_user_id))
            created_id = account.get("id")
            if not isinstance(created_id, str) or not _ACCOUNT_ID.fullmatch(created_id):
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
            is_v2 = True
        else:
            stage = "account_api"
            is_v2 = _v2_status(client, account_id) is not None
        return_url = f"{origin}/creator/payouts/return"
        refresh_url = f"{origin}/creator/payouts/refresh"
        stage = "account_link"
        if is_v2:
            link = _v2(client, "post", "/v2/core/account_links", {
                "account": account_id,
                "use_case": {
                    "type": "account_onboarding",
                    "account_onboarding": {"return_url": return_url, "refresh_url": refresh_url},
                },
            })
        else:  # an account created through Accounts v1 keeps v1 onboarding links
            link = client.v1.account_links.create(
                {
                    "account": account_id,
                    "type": "account_onboarding",
                    "return_url": return_url,
                    "refresh_url": refresh_url,
                }
            )
        stage = "account_link_url"
        url = link.get("url")
        if not _stripe_hosted(url):
            raise PayoutSetupError("Payout setup is unavailable")
        return str(url)
    except Exception as exc:
        if stage != "origin":  # _public_origin logged its own detail
            _log_failure(stage, exc)
        if isinstance(exc, PayoutSetupError):
            raise
        raise PayoutSetupError("Payout setup is unavailable") from None
