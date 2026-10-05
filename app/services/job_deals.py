"""Deals: what an accepted offer becomes.

Step 6C. Exactly one ``public.creator_job_deals`` row exists per accepted
``creator_job_offers`` row. It is created by the database itself
(migration 0051's ``creator_job_offers_create_deal`` trigger) inside the
same transaction as the Step 6B accept UPDATE, with every value copied
from the stored offer. This module therefore NEVER inserts a deal and never
takes a term, party or id from the browser; it only reads.

Access is relationship-based: a deal is visible to its ``poster_user_id``
and its ``applicant_user_id`` and to no one else, whatever their role.
Every read filters on the authenticated user id passed in by the route,
and a deal whose listing is missing or taken down is treated like that
listing (hidden), matching Step 6B's offers.

Nothing here touches payment, payouts, Stripe, completion or notifications.
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import job_applications, job_offers, profiles

logger = logging.getLogger(__name__)

STATUS_ACTIVE = "active"
# babyg's Step 6C line is a fixed statement of state -- no LLM, no workflow.
BABYG_STATE = ("Deal active.", "Payment is the next step.")

_LIST_LIMIT = 100
_COLS = (
    "id,offer_id,application_id,listing_id,poster_user_id,applicant_user_id,"
    "amount_cents,currency,deliverables,due_date,status,created_at"
)


class _DealReadError(Exception):
    """A listing/profile read failed while composing deals."""


def _rows_where(column: str, uid: str) -> list[dict[str, Any]] | None:
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_deals")
            .select(_COLS)
            .eq(column, uid)
            .order("created_at", desc=True)
            .limit(_LIST_LIMIT)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_deals.read_failed column=%s", column)
        return None
    return [dict(r) for r in (getattr(result, "data", None) or []) if str(r.get(column) or "") == uid]


def _is_party(deal: dict[str, Any], uid: str) -> bool:
    return uid in (str(deal.get("poster_user_id") or ""), str(deal.get("applicant_user_id") or ""))


def _decorate(
    deals: list[dict[str, Any]], viewer_id: str
) -> list[dict[str, Any]]:
    """Attach the OTHER party's identity, the opportunity title and display
    strings. Drops deals whose listing is missing or taken down."""
    listings = job_offers._listings_by_id([str(d.get("listing_id") or "") for d in deals])
    if listings is None:
        raise _DealReadError
    deals = [d for d in deals if job_offers._visible(listings.get(str(d.get("listing_id") or "")))]
    posters = job_offers._poster_identities(
        [listings[str(d["listing_id"])] for d in deals if str(d.get("poster_user_id")) != viewer_id]
    )
    applicant_ids = sorted(
        {str(d["applicant_user_id"]) for d in deals if str(d.get("applicant_user_id")) != viewer_id}
    )
    applicants = profiles.get_creators_by_ids(applicant_ids) if applicant_ids else {}
    out = []
    for d in deals:
        if str(d.get("poster_user_id")) == viewer_id:
            other_id = str(d.get("applicant_user_id") or "")
            ident = job_applications.applicant_identity(applicants.get(other_id), other_id)
            other = {"name": ident["name"], "image_url": ident["image_url"],
                     "initial": ident["initial"], "kind": "creator"}
        else:
            other = posters.get(str(d.get("poster_user_id") or ""), {})
        listing = listings[str(d["listing_id"])]
        out.append({
            **d,
            "viewer_is_poster": str(d.get("poster_user_id")) == viewer_id,
            "other": other,
            "listing_title": listing.get("title") or "",
            "amount_display": job_offers.format_usd(int(d.get("amount_cents") or 0)),
            "due_display": job_offers._short_date(d.get("due_date")),
        })
    return out


def list_for_user(user_id: str) -> list[dict[str, Any]] | None:
    """Deals where the user is the poster OR the applicant, newest first
    (id breaks ties so the order is deterministic). ``None`` when a read
    fails, so the page never shows a fake empty state."""
    uid = safe_uuid(user_id)
    if not uid:
        return []
    as_poster = _rows_where("poster_user_id", uid)
    as_applicant = _rows_where("applicant_user_id", uid)
    if as_poster is None or as_applicant is None:
        return None
    by_id = {str(d["id"]): d for d in as_poster + as_applicant}
    try:
        items = _decorate(list(by_id.values()), uid)
    except _DealReadError:
        return None
    items.sort(key=lambda d: str(d.get("id") or ""))
    items.sort(key=lambda d: str(d.get("created_at") or ""), reverse=True)
    return items


def get_for_user(deal_id: str, user_id: str) -> dict[str, Any] | None:
    """One deal, only for one of its two parties. ``None`` for a malformed
    id, a missing row, a non-party, a hidden listing or a read failure --
    callers 404 without revealing which."""
    did = safe_uuid(deal_id)
    uid = safe_uuid(user_id)
    if not did or not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_deals")
            .select(_COLS)
            .eq("id", did)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_deals.get_for_user.read_failed")
        return None
    rows = getattr(result, "data", None) or []
    if not rows or not _is_party(rows[0], uid) or str(rows[0].get("id")) != did:
        return None
    try:
        items = _decorate([dict(rows[0])], uid)
    except _DealReadError:
        return None
    return items[0] if items else None


def deal_id_for_offer(offer_id: str, user_id: str) -> str | None:
    """The deal created from ``offer_id``, if the user is one of its parties.
    Used for the "View deal" links on an accepted offer."""
    oid = safe_uuid(offer_id)
    uid = safe_uuid(user_id)
    if not oid or not uid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_deals")
            .select("id,poster_user_id,applicant_user_id")
            .eq("offer_id", oid)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_deals.deal_id_for_offer.read_failed")
        return None
    rows = getattr(result, "data", None) or []
    if not rows or not _is_party(rows[0], uid):
        return None
    return str(rows[0].get("id") or "") or None
