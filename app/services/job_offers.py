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
from app.services import profiles

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


# --------------------------------------------------------------------------
# Step 6B: the RECIPIENT side -- received offers, viewed state, accept/decline
# --------------------------------------------------------------------------
#
# The recipient is always the stored ``applicant_user_id``; every read and
# write below filters on it, so an offer id alone never reaches anyone else.
# Offers whose listing is missing or taken down by an operator are treated
# like that listing: hidden and not actionable.

STATUS_ACCEPTED = "accepted"
STATUS_DECLINED = "declined"
DECISIONS = (STATUS_ACCEPTED, STATUS_DECLINED)

RESPONDED = "responded"
ALREADY_DECIDED = "already_decided"
NOT_FOUND = "not_found"

_INBOX_LIMIT = 100
_RECEIVED_COLS = (
    "id,application_id,listing_id,poster_user_id,applicant_user_id,amount_cents,"
    "currency,due_date,status,viewed_at,responded_at,created_at"
)
_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
# Terms babyg checks the offer text for. If a term is never mentioned, the
# brief says so -- it never assumes what the poster meant.
_TERM_SIGNALS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("usage rights", ("usage", "license", "licence", "licensing", "whitelist", "rights")),
    ("exclusivity", ("exclusiv", "non-compete", "noncompete")),
)


def format_usd(cents: int) -> str:
    """Integer cents -> "$1,000" or "$250.50". No floating point."""
    dollars, rem = divmod(int(cents), 100)
    return f"${dollars:,}" if rem == 0 else f"${dollars:,}.{rem:02d}"


def _short_date(value: Any) -> str:
    """"2026-10-30" -> "oct 30, 2026" (the app's lowercase date style)."""
    try:
        y, m, d = str(value)[:10].split("-")
        return f"{_MONTHS[int(m) - 1]} {int(d)}, {y}"
    except (ValueError, IndexError):
        return str(value or "")


def display_status(offer: dict[str, Any]) -> str:
    """new / viewed / accepted / declined -- derived from stored columns only."""
    status = str(offer.get("status") or "")
    if status in DECISIONS:
        return status
    return "viewed" if offer.get("viewed_at") else "new"


def _listings_by_id(listing_ids: list[str]) -> dict[str, dict[str, Any]] | None:
    ids = sorted({lid for lid in (safe_uuid(i) for i in listing_ids) if lid})
    if not ids:
        return {}
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_listings")
            .select("id,title,poster_user_id,poster_role,is_taken_down")
            .in_("id", ids)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.listings_read_failed")
        return None
    return {str(r.get("id")): dict(r) for r in (getattr(result, "data", None) or [])}


def _visible(listing: dict[str, Any] | None) -> bool:
    return bool(listing) and not (listing or {}).get("is_taken_down")


def unread_count(applicant_user_id: str) -> int:
    """Received offers still awaiting a first look: status 'sent' and never
    viewed, on a listing that still exists and is not taken down. Never
    counts offers the user SENT. 0 on any failure (badges never break a
    page)."""
    uid = safe_uuid(applicant_user_id)
    if not uid:
        return 0
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .select("id,listing_id")
            .eq("applicant_user_id", uid)
            .eq("status", STATUS_SENT)
            .is_("viewed_at", "null")
            .limit(_INBOX_LIMIT)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.unread_count.read_failed")
        return 0
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return 0
    listings = _listings_by_id([str(r.get("listing_id") or "") for r in rows])
    if listings is None:
        return 0
    return sum(1 for r in rows if _visible(listings.get(str(r.get("listing_id") or ""))))


def _poster_identities(listings: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Display identity of each poster from EXISTING public profile data."""
    out: dict[str, dict[str, Any]] = {}
    creator_ids = sorted(
        {str(lst["poster_user_id"]) for lst in listings if lst.get("poster_role") != "brand"}
    )
    creators = profiles.get_creators_by_ids(creator_ids) if creator_ids else {}
    for lst in listings:
        pid = str(lst.get("poster_user_id") or "")
        if not pid or pid in out:
            continue
        if lst.get("poster_role") == "brand":
            brand = profiles.public_brand(profiles.get_brand_profile(pid)) or {}
            name = str(brand.get("company_name") or "").strip() or "brand"
            out[pid] = {"name": name, "image_url": str(brand.get("logo_url") or ""),
                        "kind": "brand", "initial": name[:1].upper()}
        else:
            c = creators.get(pid) or {}
            handle = str(c.get("instagram_handle") or "").strip().lstrip("@")
            name = str(c.get("full_name") or "").strip() or (f"@{handle}" if handle else "creator")
            out[pid] = {"name": name, "image_url": str(c.get("profile_photo_url") or ""),
                        "kind": "creator", "initial": (name.lstrip("@")[:1] or "?").upper()}
    return out


def _decorate(offer: dict[str, Any], listing: dict[str, Any], poster: dict[str, Any]) -> dict[str, Any]:
    return {
        **offer,
        "listing_title": listing.get("title") or "",
        "poster": poster,
        "amount_display": format_usd(int(offer.get("amount_cents") or 0)),
        "due_display": _short_date(offer.get("due_date")),
        "display_status": display_status(offer),
    }


def list_received(applicant_user_id: str) -> list[dict[str, Any]] | None:
    """Offers addressed TO this user, for the Offers inbox.

    Ordering: offers still awaiting a decision first, then decided ones;
    newest first within each group; id as the final tiebreak so the order
    is deterministic. Deliverables and note are NOT selected for the list.
    Returns ``None`` when the read fails (so the page never shows a fake
    empty state)."""
    uid = safe_uuid(applicant_user_id)
    if not uid:
        return []
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .select(_RECEIVED_COLS)
            .eq("applicant_user_id", uid)
            .order("created_at", desc=True)
            .limit(_INBOX_LIMIT)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.list_received.read_failed")
        return None
    rows = [dict(r) for r in (getattr(result, "data", None) or [])
            if str(r.get("applicant_user_id") or "") == uid]
    listings = _listings_by_id([str(r.get("listing_id") or "") for r in rows])
    if listings is None:
        return None
    rows = [r for r in rows if _visible(listings.get(str(r.get("listing_id") or "")))]
    posters = _poster_identities([listings[str(r["listing_id"])] for r in rows])
    items = [
        _decorate(r, listings[str(r["listing_id"])], posters.get(str(r.get("poster_user_id") or ""), {}))
        for r in rows
    ]
    items.sort(key=lambda o: str(o.get("id") or ""))
    items.sort(key=lambda o: str(o.get("created_at") or ""), reverse=True)
    items.sort(key=lambda o: 0 if o.get("status") == STATUS_SENT else 1)
    return items


def get_received(offer_id: str, applicant_user_id: str) -> dict[str, Any] | None:
    """One offer addressed to this user, with its full terms. ``None`` for a
    malformed id, someone else's offer, a missing row, a hidden listing or a
    read failure -- callers 404 without revealing which."""
    oid = safe_uuid(offer_id)
    uid = safe_uuid(applicant_user_id)
    if not oid or not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .select(_RECEIVED_COLS + ",deliverables,note")
            .eq("id", oid)
            .eq("applicant_user_id", uid)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.get_received.read_failed")
        return None
    rows = getattr(result, "data", None) or []
    if not rows:
        return None
    offer = dict(rows[0])
    if str(offer.get("applicant_user_id") or "") != uid or str(offer.get("id") or "") != oid:
        return None
    listings = _listings_by_id([str(offer.get("listing_id") or "")])
    listing = (listings or {}).get(str(offer.get("listing_id") or ""))
    if listing is None or not _visible(listing):
        return None
    poster = _poster_identities([listing]).get(str(offer.get("poster_user_id") or ""), {})
    return _decorate(offer, listing, poster)


def mark_viewed(offer_id: str, applicant_user_id: str) -> bool:
    """Record the recipient's first look. Only the recipient's own row, only
    once (``viewed_at is null``). Returns True when this call set it."""
    oid = safe_uuid(offer_id)
    uid = safe_uuid(applicant_user_id)
    if not oid or not uid:
        return False
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .update({"viewed_at": datetime.now(UTC).isoformat()})
            .eq("id", oid)
            .eq("applicant_user_id", uid)
            .is_("viewed_at", "null")
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.mark_viewed.write_failed")
        return False
    return bool(getattr(result, "data", None))


def respond(offer_id: str, applicant_user_id: str, decision: str) -> tuple[str, dict[str, Any] | None]:
    """Accept or decline -- once.

    The write is a single conditional UPDATE: ``id`` AND the recipient AND
    ``status = 'sent'``. Postgres re-checks that predicate after taking the
    row lock, so of two racing responses exactly one matches; the loser
    (and any repeat submit) changes nothing and gets ``ALREADY_DECIDED``.
    Nothing else is created: no deal, no payment, no notification."""
    if decision not in DECISIONS:
        return REFUSED, None
    offer = get_received(offer_id, applicant_user_id)
    if offer is None:
        return NOT_FOUND, None
    if offer.get("status") != STATUS_SENT:
        return ALREADY_DECIDED, offer
    now = datetime.now(UTC).isoformat()
    body: dict[str, Any] = {"status": decision, "responded_at": now}
    if not offer.get("viewed_at"):
        body["viewed_at"] = now
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_offers")
            .update(body)
            .eq("id", str(offer["id"]))
            .eq("applicant_user_id", str(offer["applicant_user_id"]))
            .eq("status", STATUS_SENT)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_offers.respond.write_failed")
        return FAILED, offer
    if not getattr(result, "data", None):
        return ALREADY_DECIDED, get_received(offer_id, applicant_user_id)
    return RESPONDED, get_received(offer_id, applicant_user_id)


def compose_brief(offer: dict[str, Any], *, today: date | None = None) -> list[str]:
    """babyg's brief for a received offer, built ONLY from the stored terms.

    Deterministic on purpose: the repo's brief surfaces never call an LLM
    during a page render, and this text must not invent anything -- no
    market rates, no fairness verdicts, no legal terms. It restates the
    amount, scope and timeline, and names common terms the offer text
    never mentions."""
    deliverables = _clean_text(offer.get("deliverables"))
    note = _clean_text(offer.get("note"))
    lines = [ln.strip(" -•\t") for ln in deliverables.split("\n") if ln.strip(" -•\t")]
    scope = ", ".join(lines)
    if len(scope) > 140:
        # Cut at the last complete item so the summary never ends mid-word.
        cut = scope[:140]
        boundary = cut.rfind(", ")
        scope = (cut[:boundary] if boundary >= 40 else cut[:139].rstrip(" ,")) + " …"
    amount = format_usd(int(offer.get("amount_cents") or 0))
    brief = [f"{amount} for {scope}, due {_short_date(offer.get('due_date'))}."]

    try:
        due = date.fromisoformat(str(offer.get("due_date"))[:10])
    except ValueError:
        due = None
    if due is not None and offer.get("status") == STATUS_SENT:
        days = (due - (today or datetime.now(UTC).date())).days
        if days < 0:
            brief.append("The due date has already passed.")
        elif days <= 1:
            brief.append("It's due within a day.")
        else:
            brief.append(f"That's {days} days from today.")

    text = f"{deliverables}\n{note}".lower()
    missing = [label for label, words in _TERM_SIGNALS if not any(w in text for w in words)]
    if missing:
        brief.append(f"The offer doesn't mention {' or '.join(missing)}.")
    if not note:
        brief.append("No note was included.")
    return brief
