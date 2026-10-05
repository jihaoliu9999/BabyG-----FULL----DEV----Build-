"""Offers from an opportunity's poster to one applicant.

Step 6A: the person who POSTED an opportunity can send ONE structured offer
for a specific application. The row lands in ``public.creator_job_offers``
(migration 0049). Nothing else happens -- no deal, no payment, no Stripe
call, no notification, no accept/decline.

Design notes:

* **Base deal value only.** ``amount_cents`` is the proposed base deal value
  the poster typed, stored as integer cents. No fees are computed or shown
  here, and nothing in this module suggests, bounds or tiers prices beyond
  an anti-abuse ceiling that mirrors the DB CHECK.
* **Server-owned identity.** The browser submits only the terms (amount,
  deliverables, due date, note). ``listing_id``, ``poster_user_id`` and
  ``applicant_user_id`` are taken from the authoritative listing /
  application rows, and the poster must equal the authenticated session
  user passed in by the route. ``currency`` and ``status`` are constants.
* **One offer per application.** ``get_for_application`` short-circuits
  the obvious duplicate; the ``creator_job_offers_one_per_application``
  unique constraint is the final guard against a racing double submit.
* **Private.** The table has RLS on and no client access; the service-role
  client is the only reader/writer. Reads here are scoped to the poster and
  return only the offer's state, never its terms.
* **No raise.** Supabase failures log and return a failure outcome so the
  route can re-render the form instead of a 500.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)

CURRENCY = "USD"
STATUS_SENT = "sent"

# Must match the CHECKs in migration 0049. The amount ceiling is an
# anti-abuse bound ($10,000,000.00), not a pricing rule.
MAX_AMOUNT_CENTS = 1_000_000_000
MAX_DELIVERABLES_CHARS = 2000
MAX_NOTE_CHARS = 2000

# Longest amount string worth parsing ("$1,000,000,000.00" is 17 chars).
_MAX_AMOUNT_INPUT_CHARS = 32
# Optional "$", then digits (optionally grouped with commas), then an
# optional "." with 1-2 digits. ASCII digits only: no signs, exponents,
# NaN/Infinity, spaces or non-ASCII numerals.
_AMOUNT_RE = re.compile(r"\$?(?P<int>[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.(?P<frac>[0-9]{1,2}))?")
_TOO_PRECISE_RE = re.compile(r"\$?[0-9][0-9,]*\.[0-9]{3,}")
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")

CREATED = "created"
DUPLICATE = "duplicate"
REFUSED = "refused"
FAILED = "failed"


@dataclass(frozen=True)
class OfferTerms:
    """Validated offer terms. The only browser-supplied part of an offer."""

    amount_cents: int
    deliverables: str
    due_date: date
    note: str | None


def _clean_text(raw: str | None) -> str:
    """Trim and normalize line endings so limits count what the user sees."""
    return str(raw or "").replace("\r\n", "\n").replace("\r", "\n").strip()


def parse_amount_cents(raw: str | None) -> tuple[int | None, str | None]:
    """Parse a USD amount string into integer cents without floating point.

    ``"1,000"`` / ``"$1,000.00"`` -> 100000; ``"250.5"`` -> 25050.
    Returns ``(cents, None)`` or ``(None, error message)``.
    """
    s = str(raw or "").strip()
    if not s:
        return None, "Enter an offer amount."
    if s.startswith("-") or s.startswith("$-"):
        return None, "Offer amount must be greater than $0."
    if len(s) > _MAX_AMOUNT_INPUT_CHARS:
        if re.fullmatch(r"\$?[0-9,]+(?:\.[0-9]*)?", s):
            return None, "Offer amount can't be more than $10,000,000.00."
        return None, "Enter a valid amount, like 1,000.00."
    m = _AMOUNT_RE.fullmatch(s)
    if m is None:
        if _TOO_PRECISE_RE.fullmatch(s):
            return None, "Offer amount can have at most 2 decimal places."
        return None, "Enter a valid amount, like 1,000.00."
    whole = m.group("int").replace(",", "").lstrip("0") or "0"
    if len(whole) > 10:
        return None, "Offer amount can't be more than $10,000,000.00."
    frac = (m.group("frac") or "").ljust(2, "0")
    cents = int(whole) * 100 + int(frac)
    if cents <= 0:
        return None, "Offer amount must be greater than $0."
    if cents > MAX_AMOUNT_CENTS:
        return None, "Offer amount can't be more than $10,000,000.00."
    return cents, None


def earliest_due_date(now: datetime | None = None) -> date:
    """The earliest due date that is not in the past for anyone.

    The poster's timezone is not known, so "today" is taken as the calendar
    date at UTC-12 -- the last place on earth to reach a date. That way a
    poster anywhere can pick their own today, and every genuinely past date
    is rejected.
    """
    current = now or datetime.now(UTC)
    return (current.astimezone(UTC) - timedelta(hours=12)).date()


def parse_due_date(raw: str | None, *, today: date) -> tuple[date | None, str | None]:
    s = str(raw or "").strip()
    if not s:
        return None, "Choose a due date."
    if not _DATE_RE.fullmatch(s):
        return None, "Enter a valid due date."
    try:
        parsed = date.fromisoformat(s)
    except ValueError:
        return None, "Enter a valid due date."
    if parsed < today:
        return None, "Due date can't be in the past."
    return parsed, None


def validate_terms(
    *,
    amount: str | None,
    deliverables: str | None,
    due_date: str | None,
    note: str | None,
    today: date | None = None,
) -> tuple[OfferTerms | None, dict[str, str]]:
    """Validate the four submitted fields. Returns ``(terms, {})`` when all
    are valid, otherwise ``(None, {field: message})`` naming every invalid
    field. Over-length text is rejected, never silently truncated."""
    errors: dict[str, str] = {}

    cents, amount_error = parse_amount_cents(amount)
    if amount_error:
        errors["amount"] = amount_error

    clean_deliverables = _clean_text(deliverables)
    if not clean_deliverables:
        errors["deliverables"] = "Describe the deliverables."
    elif len(clean_deliverables) > MAX_DELIVERABLES_CHARS:
        errors["deliverables"] = (
            f"Deliverables must be {MAX_DELIVERABLES_CHARS:,} characters or fewer."
        )

    parsed_due, due_error = parse_due_date(due_date, today=today or earliest_due_date())
    if due_error:
        errors["due_date"] = due_error

    clean_note = _clean_text(note)
    if len(clean_note) > MAX_NOTE_CHARS:
        errors["note"] = f"Note must be {MAX_NOTE_CHARS:,} characters or fewer."

    if errors or cents is None or parsed_due is None:
        return None, errors
    return (
        OfferTerms(
            amount_cents=cents,
            deliverables=clean_deliverables,
            due_date=parsed_due,
            note=clean_note or None,
        ),
        {},
    )


def can_offer(application: dict[str, Any], poster_user_id: str) -> bool:
    """False for a malformed or self relationship (poster == applicant).
    Existing data is never "fixed"; it simply cannot receive an offer."""
    applicant = safe_uuid(str(application.get("applicant_user_id") or ""))
    poster = safe_uuid(str(poster_user_id or ""))
    return bool(applicant and poster and applicant != poster)


def get_for_application(application_id: str, poster_user_id: str) -> dict[str, Any] | None:
    """The existing offer for an application, scoped to its poster.

    Returns only ``id``, ``status`` and ``created_at`` -- enough to show the
    "Offer sent" state; the terms are not read back. ``None`` when there is
    no offer, the ids are malformed, or the read fails (logged). A failed
    read can at worst show "Make offer" again; the unique constraint still
    refuses a second row.
    """
    aid = safe_uuid(application_id)
    uid = safe_uuid(poster_user_id)
    if not aid or not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .select("id,status,created_at")
            .eq("application_id", aid)
            .eq("poster_user_id", uid)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.get_for_application.read_failed application=%s", aid)
        return None
    rows = getattr(result, "data", None) or []
    return dict(rows[0]) if rows else None


def create(
    *,
    listing: dict[str, Any],
    application: dict[str, Any],
    poster_user_id: str,
    terms: OfferTerms,
) -> tuple[str, dict[str, Any] | None]:
    """Insert the one offer for ``application``.

    Returns ``(CREATED, row)``, ``(DUPLICATE, existing)`` when an offer
    already exists (including a lost race), ``(REFUSED, None)`` when the
    relationship does not hold, or ``(FAILED, None)`` on a write failure.

    The relationship is re-proved here, not assumed from the caller:

      session user (``poster_user_id``) == ``listing.poster_user_id``
      ``application.listing_id``         == ``listing.id``
      ``application.applicant_user_id``  != the poster

    and every identity column written comes from those rows.
    """
    lid = safe_uuid(str(listing.get("id") or ""))
    owner = safe_uuid(str(listing.get("poster_user_id") or ""))
    poster = safe_uuid(str(poster_user_id or ""))
    aid = safe_uuid(str(application.get("id") or ""))
    app_listing = safe_uuid(str(application.get("listing_id") or ""))
    applicant = safe_uuid(str(application.get("applicant_user_id") or ""))
    if not (lid and owner and poster and aid and app_listing and applicant):
        return REFUSED, None
    if owner != poster or app_listing != lid or applicant == poster:
        return REFUSED, None
    # Belt and braces against a future caller skipping validate_terms.
    if not (
        isinstance(terms, OfferTerms)
        and 0 < terms.amount_cents <= MAX_AMOUNT_CENTS
        and 0 < len(terms.deliverables.strip()) <= MAX_DELIVERABLES_CHARS
        and len(terms.note or "") <= MAX_NOTE_CHARS
    ):
        return REFUSED, None

    existing = get_for_application(aid, poster)
    if existing is not None:
        return DUPLICATE, existing

    body = {
        "application_id": aid,
        "listing_id": lid,
        "poster_user_id": poster,
        "applicant_user_id": applicant,
        "amount_cents": terms.amount_cents,
        "currency": CURRENCY,
        "deliverables": terms.deliverables,
        "due_date": terms.due_date.isoformat(),
        "note": terms.note,
        "status": STATUS_SENT,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        # Most likely the unique constraint firing on a racing submit.
        logger.exception("job_offers.create.write_failed application=%s", aid)
        existing = get_for_application(aid, poster)
        if existing is not None:
            return DUPLICATE, existing
        return FAILED, None
    rows = getattr(result, "data", None) or []
    return CREATED, (dict(rows[0]) if rows else None)
