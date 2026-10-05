"""Discover → Opportunities → "my opportunities".

Step 5C. The Opportunities tab on Discover has two secondary views:

  * ``explore``  — the existing opportunity browsing feed (unchanged).
  * ``mine``     — the opportunities the signed-in user is personally
                   involved with:

      applied → the opportunities they applied to (Step 5B
                ``creator_job_applications`` joined to the live
                opportunity card). Creators only: brands do not apply;
      posted  → the opportunities they posted
                (``creator_job_listings.poster_user_id``), each with a
                real applicant count. Brands AND creators can post, so
                both see their own here (Step 5D). A posted card opens
                that opportunity's applicant review.

This module owns NO tables and creates NO data. It composes two
existing sources of truth and returns card-shaped dicts that the
existing ``opportunity_card`` macro renders. Identity is always the
authenticated session's user id, passed in by the route — never a query
string or form value.

Privacy: application ``message`` text is never selected here (see
``job_applications.list_for_applicant`` / ``count_by_listing``), so it
cannot reach a list surface. No applicant identities are exposed — posters
only receive a number; the applicant list itself lives behind the
owner-checked review routes (``job_applications.authorize_poster``).
"""

from __future__ import annotations

import logging
from typing import Any

from app.services import discover, job_applications, jobs

logger = logging.getLogger(__name__)

VIEW_EXPLORE = "explore"
VIEW_MINE = "mine"

# Upper bound on rows composed per request. Both lists are personal (one
# user's applications / one poster's listings), so this is generous.
_MAX_ITEMS = 100


def clean_view(value: str | None) -> str:
    """Closed vocabulary: anything but exactly ``mine`` is ``explore``."""
    return VIEW_MINE if str(value or "").strip().lower() == VIEW_MINE else VIEW_EXPLORE


def applicant_label(count: int | None) -> str:
    """"3 applicants" / "1 applicant" / "0 applicants"; empty when the
    count is unknown so the UI never shows a made-up number."""
    if count is None:
        return ""
    return f"{count} applicant" if count == 1 else f"{count} applicants"


def creator_items(user_id: str) -> list[dict[str, Any]]:
    """Opportunities this creator has applied to, most recent first.

    Source of truth: ``creator_job_applications`` rows where
    ``applicant_user_id`` is the authenticated creator. Each is joined to
    the same normalized public card the Explore feed renders.

    A listing that is no longer publicly viewable (closed, expired,
    taken down) is omitted: the existing opportunity detail page 404s
    for non-posters in that state, so listing it would produce a dead
    link.
    """
    applications = job_applications.list_for_applicant(user_id, limit=_MAX_ITEMS)
    if not applications:
        return []
    listing_ids = [str(a["listing_id"]) for a in applications if a.get("listing_id")]
    cards = discover.get_opportunity_cards(listing_ids)
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for application in applications:
        listing_id = str(application.get("listing_id") or "")
        if not listing_id or listing_id in seen:
            continue
        seen.add(listing_id)
        card = cards.get(listing_id)
        if card is None:
            continue
        items.append(
            {
                **card,
                "applied": True,
                "applied_at": application.get("created_at"),
            }
        )
    return items


def poster_items(
    user_id: str,
    *,
    detail_prefix: str,
    detail_suffix: str = "",
    fallback_location: str | None = None,
) -> list[dict[str, Any]]:
    """Opportunities this user posted, newest first, each with a real
    applicant count.

    ``detail_prefix`` + listing id + ``detail_suffix`` is the card's link;
    Step 5D passes ``"/applicants"`` so a posted opportunity opens its
    applicant review. Both parts are server-chosen constants.

    Ownership is ``creator_job_listings.poster_user_id`` — the
    authoritative column. It is intentionally NOT filtered on
    ``poster_role``: rows written before role persistence was fixed carry
    a default role even when a brand posted them, but ``poster_user_id``
    has always been correct.

    Applicant counts are requested only for the ids returned by
    ``jobs.list_by_poster(user_id)``, i.e. listings this user owns. If
    the count lookup fails the items carry ``applicant_count=None`` and
    the UI omits the number rather than showing 0.
    """
    rows = [
        r
        for r in jobs.list_by_poster(user_id, limit=_MAX_ITEMS)
        if str(r.get("poster_user_id") or "") == user_id and not r.get("is_taken_down")
    ]
    if not rows:
        return []
    counts = job_applications.count_by_listing([str(r["id"]) for r in rows])
    items: list[dict[str, Any]] = []
    for row in rows:
        listing_id = str(row["id"])
        location = ", ".join(
            p for p in (row.get("location_city"), row.get("location_region")) if p
        )
        count = counts.get(listing_id) if counts is not None else None
        items.append(
            {
                "card_kind": "opportunity",
                "card_id": listing_id,
                "title": row.get("title"),
                # The poster is the viewer; repeating "posted by <me>" on
                # every card would be noise.
                "subtitle": None,
                "location_label": location or fallback_location,
                "deadline": row.get("deadline"),
                "description": row.get("description"),
                "budget_min": row.get("budget_min"),
                "compensation_text": row.get("compensation_text"),
                "tags": [
                    str(t) for t in (row.get("target_niches") or []) if str(t).strip()
                ],
                "detail_path": f"{detail_prefix}{listing_id}{detail_suffix}",
                "is_active": bool(row.get("is_active")),
                "applicant_count": count,
                "applicant_label": applicant_label(count),
            }
        )
    return items


def apply_filters(
    items: list[dict[str, Any]],
    *,
    category: str | None,
    location: str | None,
    budget_min: int | None,
    budget_max: int | None,
) -> list[dict[str, Any]]:
    """The Explore filter semantics, applied to an already-personal list.

    Mirrors ``discover.list_cards``: ``category`` is an exact (lowercased)
    tag match, ``location`` a case-insensitive substring of the location
    label, and the budget bounds are range-overlap tests that exclude
    listings with no stated budget when a bound is set.
    """
    out = items
    if category:
        needle = category.strip().lower()[:40]
        out = [i for i in out if needle in [str(t).lower() for t in i.get("tags") or []]]
    if location:
        needle = location.strip().lower()[:80]
        out = [i for i in out if needle in str(i.get("location_label") or "").lower()]
    if budget_min is not None:
        floor = max(0, budget_min)
        out = [
            i
            for i in out
            if i.get("budget_max") is not None and i["budget_max"] >= floor
        ]
    if budget_max is not None:
        ceiling = max(0, budget_max)
        out = [
            i
            for i in out
            if i.get("budget_min") is not None and i["budget_min"] <= ceiling
        ]
    return out
