"""Step 7A: the payer funds an active deal through Stripe Checkout (sandbox).

Who pays: the deal's poster (``poster_user_id``), and only them. The
recipient is the applicant creator. Both come from the stored deal row,
never from the browser.

Economics (locked, integer cents, computed here from the deal's base
value -- the accepted offer's amount):

    payer fee      = 10% of base (half-up)      payer total    = base + fee
    recipient fee  = 10% of base (half-up)      recipient gets = base - fee
    babyg gross    = payer fee + recipient fee  (before Stripe costs)

Routing: a Stripe Connect DESTINATION charge, checked against the installed
SDK (stripe 12.5.1, API 2025-08-27.basil): a platform Checkout Session with
``payment_intent_data.transfer_data.destination`` = the recipient's
connected account and ``application_fee_amount`` = babyg's two fees, so
Stripe moves base - 10% to the recipient's account when the charge
succeeds. The recipient must have finished payout setup (Stripe reports
the account ready) before a session is created. No escrow, no release,
no refunds here.

Funding is webhook-only. The browser's return from Stripe proves nothing:
only a signature-verified ``checkout.session.*`` event moves a payment to
``succeeded``, and migration 0052's trigger funds the deal in that same
transaction. Every transition is a conditional UPDATE (``status =
'pending'``), so replays and retries change nothing twice; the database
also refuses a second open payment per deal, tampered amounts or parties
(composite foreign key + fee CHECK) and any change to a final payment.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import creator_payouts, deal_events, job_deals, job_offers
from app.services.stripe_client import safe_error_fields

logger = logging.getLogger(__name__)

TABLE = "creator_job_deal_payments"
FEE_BPS = 1_000  # 10%, each side
STRIPE_MIN_CENTS = 50
STRIPE_MAX_CENTS = 99_999_999  # Stripe's largest single card charge ($999,999.99)
_STALE_ATTEMPT = timedelta(minutes=10)

STATUS_PENDING = "pending"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"
DEAL_FUNDED = "funded"

# babyg's line on a funded deal (Step 6C's BABYG_STATE covers active deals).
FUNDED_STATE = ("Payment confirmed. Work can begin.",)

# start_checkout outcomes
CHECKOUT = "checkout"
NOT_FOUND = "not_found"
ALREADY_FUNDED = "funded"
PROCESSING = "processing"
RECIPIENT_NOT_READY = "recipient"
OUT_OF_RANGE = "limit"
UNAVAILABLE = "error"

# Stripe events that reach business logic. Everything else is acked untouched.
EVENT_COMPLETED = "checkout.session.completed"
EVENT_ASYNC_SUCCEEDED = "checkout.session.async_payment_succeeded"
EVENT_ASYNC_FAILED = "checkout.session.async_payment_failed"
EVENT_EXPIRED = "checkout.session.expired"
HANDLED_EVENTS = frozenset({EVENT_COMPLETED, EVENT_ASYNC_SUCCEEDED, EVENT_ASYNC_FAILED, EVENT_EXPIRED})

_PAYMENT_COLS = (
    "id,deal_id,payer_user_id,recipient_user_id,base_amount_cents,total_amount_cents,"
    "application_fee_cents,recipient_stripe_account_id,currency,status,"
    "stripe_checkout_session_id,created_at"
)


class WebhookRetryError(Exception):
    """A transient failure: answer non-2xx so Stripe delivers the event again."""


class _ReadError(Exception):
    pass


@dataclass(frozen=True)
class Breakdown:
    base_cents: int
    payer_fee_cents: int
    recipient_fee_cents: int
    total_cents: int
    recipient_cents: int
    platform_fee_cents: int

    @property
    def payable(self) -> bool:
        return STRIPE_MIN_CENTS <= self.total_cents <= STRIPE_MAX_CENTS


def fee_cents(base_cents: int) -> int:
    """10% of ``base_cents``, rounded half-up to a whole cent."""
    return (base_cents * FEE_BPS + 5_000) // 10_000


def breakdown(base_cents: int) -> Breakdown:
    if isinstance(base_cents, bool) or not isinstance(base_cents, int) or base_cents <= 0:
        raise ValueError("base_cents must be a positive integer")
    fee = fee_cents(base_cents)
    return Breakdown(
        base_cents=base_cents,
        payer_fee_cents=fee,
        recipient_fee_cents=fee,
        total_cents=base_cents + fee,
        recipient_cents=base_cents - fee,
        platform_fee_cents=fee + fee,
    )


def _log(stage: str, exc: BaseException | None = None, **context: Any) -> None:
    fields: dict[str, Any] = {"stage": stage, **context}
    if exc is not None:
        fields.update(safe_error_fields(exc))
    logger.warning(
        "deal_payments.failed %s", " ".join(f"{k}={v!r}" for k, v in fields.items())
    )


# ------------------------------------------------------------------ storage


def _payments() -> Any:
    return supabase_client.get_service_client().table(TABLE)


def _open_payment(deal_id: str) -> dict[str, Any] | None:
    """The deal's one pending-or-succeeded payment, if any."""
    try:
        result = (
            _payments()
            .select(_PAYMENT_COLS)
            .eq("deal_id", deal_id)
            .in_("status", [STATUS_PENDING, STATUS_SUCCEEDED])
            .limit(1)
            .execute()
        )
    except PostgrestAPIError as exc:
        _log("payment_read", exc)
        raise _ReadError from None
    rows = getattr(result, "data", None) or []
    return dict(rows[0]) if rows else None


def _payment_by_id(payment_id: str) -> dict[str, Any] | None:
    try:
        result = _payments().select(_PAYMENT_COLS).eq("id", payment_id).limit(1).execute()
    except PostgrestAPIError as exc:
        _log("payment_read", exc)
        raise _ReadError from None
    rows = getattr(result, "data", None) or []
    return dict(rows[0]) if rows and str(rows[0].get("id")) == payment_id else None


def _insert_pending(deal: dict[str, Any], b: Breakdown, account_id: str) -> dict[str, Any] | None:
    """A new pending attempt. ``None`` when another request's attempt won
    the one-open-payment-per-deal index (the caller re-reads)."""
    row = {
        "deal_id": str(deal["id"]),
        "payer_user_id": str(deal["poster_user_id"]),
        "recipient_user_id": str(deal["applicant_user_id"]),
        "base_amount_cents": b.base_cents,
        "payer_fee_cents": b.payer_fee_cents,
        "recipient_fee_cents": b.recipient_fee_cents,
        "total_amount_cents": b.total_cents,
        "recipient_amount_cents": b.recipient_cents,
        "application_fee_cents": b.platform_fee_cents,
        "currency": "usd",
        "recipient_stripe_account_id": account_id,
        "status": STATUS_PENDING,
    }
    try:
        result = _payments().insert(row).execute()
    except PostgrestAPIError as exc:
        if str(getattr(exc, "code", "") or "") == "23505":
            return None
        _log("payment_insert", exc)
        raise _ReadError from None
    rows = getattr(result, "data", None) or []
    if not rows:
        raise _ReadError
    return dict(rows[0])


def _retire(payment_id: str, status: str) -> None:
    """pending -> failed/expired, only while still pending."""
    try:
        _payments().update({"status": status}).eq("id", payment_id).eq("status", STATUS_PENDING).execute()
    except PostgrestAPIError as exc:
        _log("payment_retire", exc, to=status)
        raise _ReadError from None


def _stale(row: dict[str, Any]) -> bool:
    try:
        created = datetime.fromisoformat(str(row.get("created_at") or "").replace("Z", "+00:00"))
    except ValueError:
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return datetime.now(UTC) - created > _STALE_ATTEMPT


# ----------------------------------------------------------------- checkout


def _checkout_url(session: Any) -> str | None:
    url = session.get("url")
    parsed = urlsplit(url) if isinstance(url, str) else None
    if not parsed or parsed.scheme != "https" or parsed.hostname != "checkout.stripe.com":
        return None
    return url


def _session_params(
    deal: dict[str, Any], payment: dict[str, Any], b: Breakdown, origin: str, return_path: str
) -> dict[str, Any]:
    did, pid = str(deal["id"]), str(payment["id"])
    metadata = {"babyg_deal_id": did, "babyg_payment_id": pid}
    return {
        "mode": "payment",
        "payment_method_types": ["card"],
        "line_items": [
            {"quantity": 1, "price_data": {"currency": "usd", "unit_amount": b.base_cents,
                                           "product_data": {"name": "Deal value"}}},
            {"quantity": 1, "price_data": {"currency": "usd", "unit_amount": b.payer_fee_cents,
                                           "product_data": {"name": "babyg fee"}}},
        ],
        "client_reference_id": did,
        "metadata": metadata,
        "payment_intent_data": {
            "application_fee_amount": b.platform_fee_cents,
            "transfer_data": {"destination": str(payment["recipient_stripe_account_id"])},
            "transfer_group": f"deal_{did}",
            "metadata": metadata,
        },
        "success_url": f"{origin}{return_path}?payment=processing",
        "cancel_url": f"{origin}{return_path}",
    }


def _create_session(
    client: Any, origin: str, deal: dict[str, Any], payment: dict[str, Any], b: Breakdown,
    return_path: str, *, owned: bool,
) -> tuple[str, str | None]:
    """Create (or, for the same attempt, re-fetch via the idempotency key)
    the attempt's Checkout Session, then record its id on the row."""
    pid = str(payment["id"])
    try:
        session = client.v1.checkout.sessions.create(
            _session_params(deal, payment, b, origin, return_path),
            options={"idempotency_key": f"babyg-deal-checkout-{pid}"},
        )
        session_id = session.get("id")
        url = _checkout_url(session)
        if not isinstance(session_id, str) or not session_id.startswith("cs_") or url is None:
            raise ValueError("unexpected Checkout Session response")
        if session.get("amount_total") not in (None, b.total_cents):
            raise ValueError("Checkout Session total does not match the deal")
    except Exception as exc:
        _log("checkout_create", exc, owned=owned)
        # Retire our own failed attempt so the next Pay is a fresh attempt
        # with a fresh idempotency key (Stripe replays a failed key for 24h).
        if owned or _stale(payment):
            with contextlib.suppress(_ReadError):
                _retire(pid, STATUS_FAILED)
        return UNAVAILABLE, None
    try:
        _payments().update({"stripe_checkout_session_id": session_id}).eq("id", pid).eq(
            "status", STATUS_PENDING
        ).is_("stripe_checkout_session_id", "null").execute()
    except PostgrestAPIError as exc:
        # The webhook can still match this attempt by its metadata.
        _log("session_attach", exc)
    return CHECKOUT, url


def start_checkout(deal_id: str, payer_user_id: str, *, return_path: str) -> tuple[str, str | None]:
    """Begin (or resume) funding for one deal. Returns (outcome, url); url
    is the Stripe-hosted Checkout page only when outcome is CHECKOUT."""
    deal = job_deals.get_for_user(deal_id, payer_user_id)
    if deal is None or not deal.get("viewer_is_poster"):
        return NOT_FOUND, None
    if deal.get("status") == DEAL_FUNDED:
        return ALREADY_FUNDED, None
    if deal.get("status") != job_deals.STATUS_ACTIVE:
        return UNAVAILABLE, None
    b = breakdown(int(deal["amount_cents"]))
    if not b.payable:
        return OUT_OF_RANGE, None
    try:
        client = creator_payouts._client()  # sandbox (sk_test_) only
        origin = creator_payouts._public_origin()
    except Exception as exc:
        _log("config", exc)
        return UNAVAILABLE, None
    did = str(deal["id"])
    try:
        for _ in range(3):
            payment = _open_payment(did)
            if payment is None:
                account_id = creator_payouts.ready_account_id(str(deal["applicant_user_id"]))
                if account_id is None:
                    return RECIPIENT_NOT_READY, None
                payment = _insert_pending(deal, b, account_id)
                if payment is None:
                    continue  # a concurrent Pay won; use its attempt
                return _create_session(client, origin, deal, payment, b, return_path, owned=True)
            if payment.get("status") == STATUS_SUCCEEDED:
                return ALREADY_FUNDED, None
            session_id = payment.get("stripe_checkout_session_id")
            if not session_id:
                return _create_session(client, origin, deal, payment, b, return_path, owned=False)
            try:
                session = client.v1.checkout.sessions.retrieve(str(session_id))
            except Exception as exc:
                _log("checkout_retrieve", exc)
                return UNAVAILABLE, None
            state = session.get("status")
            if state == "open":
                url = _checkout_url(session)
                return (CHECKOUT, url) if url else (UNAVAILABLE, None)
            if state == "complete":
                return PROCESSING, None  # paid; the webhook funds the deal
            _retire(str(payment["id"]), STATUS_EXPIRED)
    except _ReadError:
        return UNAVAILABLE, None
    return UNAVAILABLE, None


def pay_redirect(deal_id: str, payer_user_id: str, *, brand: bool) -> str | None:
    """Where the Pay POST goes next: Stripe Checkout, or back to the Deal
    page with a notice. ``None`` means 404 (not this user's deal to pay)."""
    did = safe_uuid(deal_id)
    if not did:
        return None
    deal_path = deal_events.deal_path(did, brand=brand)
    outcome, url = start_checkout(did, payer_user_id, return_path=deal_path)
    if outcome == NOT_FOUND:
        return None
    if outcome == CHECKOUT and url:
        return url
    if outcome == ALREADY_FUNDED:
        return deal_path
    return f"{deal_path}?payment={outcome}"


def notice_from_query(value: str | None) -> str | None:
    """Only the notices this module itself sends back."""
    return value if value in (PROCESSING, UNAVAILABLE, RECIPIENT_NOT_READY, OUT_OF_RANGE) else None


# ------------------------------------------------------------- Deal Detail


def detail_context(deal: dict[str, Any], viewer_id: str, *, notice: str | None) -> dict[str, Any]:
    """What the Deal page shows about payment, for THIS viewer."""
    b = breakdown(int(deal["amount_cents"]))
    fmt = job_offers.format_usd
    is_payer = bool(deal.get("viewer_is_poster"))
    funded = deal.get("status") == DEAL_FUNDED
    other = (deal.get("other") or {}).get("name") or ("the creator" if is_payer else "the poster")
    rows = [("Deal value", fmt(b.base_cents)), ("babyg fee", fmt(b.payer_fee_cents))]
    rows.append(("Total", fmt(b.total_cents)) if is_payer else ("Expected amount", fmt(b.recipient_cents)))
    funding: dict[str, Any] = {
        "is_payer": is_payer,
        "funded": funded,
        "label": "Funded" if funded else ("Payment required" if is_payer else "Awaiting payment"),
        "rows": rows,
        "pay_label": None,
        "note": None,
        "setup_payouts": False,
    }
    if funded:
        return {"funding": funding, "babyg_state": FUNDED_STATE}
    babyg_state = job_deals.BABYG_STATE
    if is_payer:
        if notice == PROCESSING:
            funding["note"] = "Stripe is confirming your payment. Refresh in a moment."
            return {"funding": funding, "babyg_state": babyg_state}
        if not b.payable:
            funding["note"] = "This total is outside the card payment limit."
            return {"funding": funding, "babyg_state": babyg_state}
        status, _account = creator_payouts.readiness(str(deal.get("applicant_user_id") or ""))
        if status in ("not_set_up", "incomplete"):
            funding["note"] = f"{other} needs to finish payout setup before you can pay."
            return {"funding": funding, "babyg_state": babyg_state}
        funding["pay_label"] = f"Pay {fmt(b.total_cents)}"
        if notice == UNAVAILABLE:
            funding["note"] = "couldn't open payment. try again."
    else:
        status, _account = creator_payouts.readiness(viewer_id)
        if status in ("not_set_up", "incomplete"):
            funding["note"] = f"Set up payouts so {other} can pay."
            funding["setup_payouts"] = True
    return {"funding": funding, "babyg_state": babyg_state}


# ----------------------------------------------------------------- webhook


def _obj_get(obj: Any, key: str) -> Any:
    try:
        return obj.get(key)
    except AttributeError:
        return None


def handle_stripe_event(event: dict[str, Any]) -> str:
    """Apply one signature-verified Stripe event. Returns a short outcome for
    the log. Raises WebhookRetryError only for transient failures (Stripe will
    redeliver; every step is idempotent). Events that are not babyg deal
    Checkout events are ignored without touching the database."""
    event_type = str(event.get("type") or "")
    if event_type not in HANDLED_EVENTS:
        return "ignored"
    obj = (event.get("data") or {}).get("object") or {}
    metadata = _obj_get(obj, "metadata") or {}
    payment_id = safe_uuid(str(metadata.get("babyg_payment_id") or ""))
    deal_id = safe_uuid(str(metadata.get("babyg_deal_id") or ""))
    if not payment_id or not deal_id:
        return "not_babyg"
    if event.get("livemode") is True:
        _log("webhook_livemode", event_type=event_type)
        return "ignored_livemode"
    session_id = str(_obj_get(obj, "id") or "")
    event_id = str(event.get("id") or "")
    try:
        payment = _payment_by_id(payment_id)
        if payment is None or str(payment.get("deal_id")) != deal_id:
            _log("webhook_unknown_payment", event_type=event_type, event_id=event_id)
            return "unknown_payment"
        stored = payment.get("stripe_checkout_session_id")
        if not session_id.startswith("cs_") or (stored and stored != session_id):
            _log("webhook_session_mismatch", event_type=event_type, event_id=event_id)
            return "session_mismatch"
        if event_type in (EVENT_EXPIRED, EVENT_ASYNC_FAILED):
            _retire(payment_id, STATUS_EXPIRED if event_type == EVENT_EXPIRED else STATUS_FAILED)
            return "retired"
        if event_type == EVENT_COMPLETED and _obj_get(obj, "payment_status") != "paid":
            return "awaiting_async_payment"
        if (
            _obj_get(obj, "mode") != "payment"
            or _obj_get(obj, "amount_total") != payment.get("total_amount_cents")
            or str(_obj_get(obj, "currency") or "").lower() != "usd"
        ):
            _log("webhook_amount_mismatch", event_type=event_type, event_id=event_id)
            return "amount_mismatch"
        result = (
            _payments()
            .update({
                "status": STATUS_SUCCEEDED,
                "succeeded_at": datetime.now(UTC).isoformat(),
                "stripe_checkout_session_id": session_id,
                "stripe_payment_intent_id": str(_obj_get(obj, "payment_intent") or "") or None,
                "last_stripe_event_id": event_id or None,
            })
            .eq("id", payment_id)
            .eq("status", STATUS_PENDING)
            .execute()
        )
        if not (getattr(result, "data", None) or []):
            current = _payment_by_id(payment_id)
            if current is None or current.get("status") != STATUS_SUCCEEDED:
                _log("webhook_paid_but_not_pending", event_id=event_id,
                     status=(current or {}).get("status"))
                return "conflict"
            outcome = "duplicate"
        else:
            outcome = "funded"
    except (_ReadError, PostgrestAPIError) as exc:
        if isinstance(exc, PostgrestAPIError):
            _log("webhook_write", exc, event_type=event_type)
        raise WebhookRetryError from None
    deal_events.record_funded(deal_id)  # deduped; heals on redelivery
    return outcome
