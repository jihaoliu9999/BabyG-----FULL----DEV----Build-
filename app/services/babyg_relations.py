"""babyg relationship threading. Phase 6 of the babyg AI v2 plan.

One brand relationship shows up on many surfaces: a DM from
@vansbrand, an email from team@vans.example, a calendar event with the
Vans marketing lead, a contract PDF. Without a resolver, each of those
opens a new deal row and babyg's memory shatters into duplicates.

This module maps any identity signal (handle, email, peer_id, brand
name) to the same open deal, and merges newly seen signals into that
deal's identity so the next touch on any surface still lands correctly.

It also owns relationship_notes: what babyg remembers about how a
brand or person behaves in business terms. Notes live longer than
deals; a "Studio Ferm ghosts past 30 days" note survives every past
deal with Studio Ferm.

Every helper is best-effort. Supabase failures never raise into the
turn; they log and return None / [].
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import babyg_deals

logger = logging.getLogger(__name__)

RelationshipNoteKind = Literal[
    "payment_reliability",
    "ghost_history",
    "contact_person",
    "past_deal_summary",
    "trust_flag",
    "other",
]

_NOTE_KINDS: frozenset[str] = frozenset(
    {
        "payment_reliability",
        "ghost_history",
        "contact_person",
        "past_deal_summary",
        "trust_flag",
        "other",
    }
)


def _service():
    return supabase_client.get_service_client()


def _normalize(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _normalize_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _normalize_handle(value: str | None) -> str:
    # Instagram, TikTok, etc. all render handles with leading @; strip
    # so "vansbrand" and "@vansbrand" resolve to the same identity.
    return (value or "").strip().lstrip("@").lower()


# ---- resolve --------------------------------------------------------------


def resolve(
    creator_id: str,
    *,
    brand_name: str | None = None,
    handle: str | None = None,
    email: str | None = None,
    peer_id: str | None = None,
) -> dict[str, Any] | None:
    """Given any identity signal, return the newest open deal that
    matches, or None if we don't know this counterparty yet. Checks
    brand_name, handles, emails, and (once the brand-side ships)
    brand_id / peer_id — whichever the caller passed. The first match
    wins in the order: exact brand_name, handle, email. Terminal deals
    are ignored.

    Callers typically pass everything they have. e.g. a DM comes in
    with a peer_id and a handle; an email comes in with an address.
    """
    creator_uid = safe_uuid(creator_id)
    if not creator_uid:
        return None
    brand_key = _normalize(brand_name)
    handle_key = _normalize_handle(handle)
    email_key = _normalize_email(email)
    peer_uid = safe_uuid(peer_id) if peer_id else None
    if not any((brand_key, handle_key, email_key, peer_uid)):
        return None

    try:
        rows = list(
            _service()
            .table("babyg_memory_deals")
            .select("*")
            .eq("creator_id", creator_uid)
            .order("last_touch_at", desc=True)
            .limit(200)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.info("babyg_relations.resolve_query_failed", exc_info=True)
        return None

    for row in rows:
        if str(row.get("stage") or "") in babyg_deals.TERMINAL_STAGES:
            continue
        if brand_key and _normalize(row.get("brand_name")) == brand_key:
            return row
        handles = {_normalize_handle(h) for h in (row.get("handles") or [])}
        if handle_key and handle_key in handles:
            return row
        emails = {_normalize_email(e) for e in (row.get("emails") or [])}
        if email_key and email_key in emails:
            return row
    return None


def learn_identity(
    deal_id: str,
    *,
    creator_id: str,
    handle: str | None = None,
    email: str | None = None,
) -> dict[str, Any] | None:
    """Record a new handle or email on an existing deal so the resolver
    finds it next time. No-op if the signal is already known. Refuses
    on terminal deals (a paid Vans deal must not attract new touches on
    the resurrected relationship — that is the next deal's job)."""
    handle_key = _normalize_handle(handle) if handle else ""
    email_key = _normalize_email(email) if email else ""
    if not handle_key and not email_key:
        return None
    deal = babyg_deals.get_deal(deal_id, creator_id=creator_id)
    if deal is None:
        return None
    if babyg_deals.is_terminal(str(deal.get("stage") or "")):
        return None
    handles = list(deal.get("handles") or [])
    emails = list(deal.get("emails") or [])
    if handle_key:
        known = {_normalize_handle(h) for h in handles}
        if handle_key not in known:
            handles.append(handle_key)
    if email_key:
        known_e = {_normalize_email(e) for e in emails}
        if email_key not in known_e:
            emails.append(email_key)
    if handles == list(deal.get("handles") or []) and emails == list(
        deal.get("emails") or []
    ):
        return deal
    try:
        (
            _service()
            .table("babyg_memory_deals")
            .update({"handles": handles, "emails": emails})
            .eq("id", deal["id"])
            .execute()
        )
        deal["handles"] = handles
        deal["emails"] = emails
        return deal
    except Exception:
        logger.info("babyg_relations.learn_identity_write_failed", exc_info=True)
        return None


# ---- thread ---------------------------------------------------------------


def thread_touchpoint(
    creator_id: str,
    *,
    kind: str,
    summary: str,
    brand_name: str | None = None,
    handle: str | None = None,
    email: str | None = None,
    peer_id: str | None = None,
    direction: str | None = None,
    source_id: str | None = None,
    stated_amount_dollars: float | int | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any] | None:
    """The Phase 6 entry point. Resolve identity, find or open a deal,
    merge the new identity signal into the deal, then log a touchpoint.

    Returns the deal that received the touchpoint, or None if we
    couldn't identify the counterparty at all (no brand, no handle, no
    email). Callers that need the touchpoint row can pull it via
    babyg_deals.list_touchpoints — thread_touchpoint's contract is
    "the deal is now up to date", not "here's the row".
    """
    if not any((brand_name and brand_name.strip(), handle, email, peer_id)):
        return None

    deal = resolve(
        creator_id,
        brand_name=brand_name,
        handle=handle,
        email=email,
        peer_id=peer_id,
    )
    if deal is None:
        # Never seen this brand before. Open a fresh deal seeded with
        # whatever identity we have. brand_name falls back to the
        # handle so we can still show the creator SOMETHING in the
        # pipeline until a real brand name arrives.
        seed_brand = (brand_name or "").strip() or handle or ""
        if not seed_brand:
            return None
        handles = [_normalize_handle(handle)] if handle else []
        emails = [_normalize_email(email)] if email else []
        deal = babyg_deals.find_or_create_deal(
            creator_id,
            brand_name=seed_brand,
            handles=handles or None,
            emails=emails or None,
        )
        if deal is None:
            return None
    else:
        # Deal already exists; make sure the new signal is recorded so
        # a future touch on a different surface still lands here.
        if handle or email:
            learn_identity(
                deal["id"],
                creator_id=creator_id,
                handle=handle,
                email=email,
            )
    babyg_deals.add_touchpoint(
        deal["id"],
        creator_id,
        kind=kind,
        summary=summary,
        direction=direction,
        source_id=source_id,
        stated_amount_dollars=stated_amount_dollars,
        occurred_at=occurred_at,
    )
    # Return fresh copy so the caller sees updated last_touch_at.
    return babyg_deals.get_deal(deal["id"], creator_id=creator_id)


# ---- relationship notes ---------------------------------------------------


def save_relationship_note(
    creator_id: str,
    *,
    kind: str,
    body: str,
    brand_name: str | None = None,
    brand_id: str | None = None,
    peer_id: str | None = None,
    babyg_source: str | None = None,
) -> dict[str, Any] | None:
    """Record a note about how a brand or person behaves in business
    terms. e.g. "Vans paid on time in q3", "Studio Ferm ghosts past 30
    days", "Olipop's marketing lead is Anna".

    At least one of brand_name / brand_id / peer_id must be present so
    the note is retrievable. Refuses otherwise; refuses unknown kinds.
    """
    creator_uid = safe_uuid(creator_id)
    if not creator_uid:
        return None
    if kind not in _NOTE_KINDS:
        logger.info("babyg_relations.note_bad_kind kind=%s", kind)
        return None
    if not (body or "").strip():
        return None
    brand_uid = safe_uuid(brand_id) if brand_id else None
    peer_uid = safe_uuid(peer_id) if peer_id else None
    brand = (brand_name or "").strip() or None
    if not brand and not brand_uid and not peer_uid:
        logger.info("babyg_relations.note_missing_target")
        return None
    row: dict[str, Any] = {
        "creator_id": creator_uid,
        "kind": kind,
        "body": body.strip(),
    }
    if brand:
        row["brand_name"] = brand
    if brand_uid:
        row["brand_id"] = brand_uid
    if peer_uid:
        row["peer_id"] = peer_uid
    if babyg_source:
        row["babyg_source"] = babyg_source.strip()[:200]
    try:
        result = (
            _service()
            .table("babyg_memory_relationship_notes")
            .insert(row)
            .execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        logger.info("babyg_relations.save_note_failed", exc_info=True)
        return None


def list_relationship_notes(
    creator_id: str,
    *,
    brand_name: str | None = None,
    peer_id: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Return notes for the creator, newest first. Optional brand_name
    (case-insensitive substring match) or peer_id filter."""
    creator_uid = safe_uuid(creator_id)
    if not creator_uid:
        return []
    limit = max(1, min(int(limit or 10), 100))
    try:
        query = (
            _service()
            .table("babyg_memory_relationship_notes")
            .select("*")
            .eq("creator_id", creator_uid)
            .order("created_at", desc=True)
            .limit(limit)
        )
        if kind and kind in _NOTE_KINDS:
            query = query.eq("kind", kind)
        if peer_id:
            peer_uid = safe_uuid(peer_id)
            if peer_uid:
                query = query.eq("peer_id", peer_uid)
        rows = list(query.execute().data or [])
    except Exception:
        logger.info("babyg_relations.list_notes_failed", exc_info=True)
        return []
    if brand_name:
        needle = _normalize(brand_name)
        if needle:
            rows = [
                r
                for r in rows
                if needle in _normalize(r.get("brand_name"))
            ]
    return rows
