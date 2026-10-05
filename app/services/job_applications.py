"""Creator applications to opportunity listings.

Step 5B: a creator viewing an opportunity on `/creator/jobs/{id}` can tap
Apply, write one short message, and submit. The row lands in
`public.creator_job_applications` (migration 0048). Nothing else happens
— no DM, no connection request, no deal, no Stripe call.

Design notes:

* **Server-owned identity.** Every write pulls `applicant_user_id` from
  the authenticated session at the route layer and passes it in
  explicitly here. This module never trusts a column named like
  `applicant_user_id` arriving from any form.
* **Private read surface.** The table has RLS enabled with zero policies
  granted to the `authenticated` role; the service-role client is the
  only reader. This module intentionally does not expose a "list all
  applications for a listing" helper — an applicant-review surface
  needs its own explicit server-side owner check and is a later step.
  The only reads exposed today are (a) ``has_applied`` and
  ``list_for_applicant``, which are scoped to the caller's OWN user id,
  and (b) ``count_by_listing``, which returns bare integers. None of
  them ever selects the ``message`` column.
* **Duplicate safety at two layers.** `has_applied` short-circuits the
  obvious duplicate for a clean UX; the unique index
  ``creator_job_applications_once_per_creator`` is the final guard
  against concurrent submits racing past the pre-check.
* **No raise.** Supabase failures log and return False / None so a
  single flaky write never surfaces as a 500 — the route re-renders
  the form with a banner.

Future steps may add owner-side read helpers; keep them in this module
rather than exposing a generic "SELECT * FROM creator_job_applications"
anywhere else.
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)

# Must match the CHECK on migration 0048. Service-side bound so the
# route surfaces a validation error BEFORE PostgREST returns a 400 for
# the DB constraint — same number both places so there is one
# authoritative limit.
MAX_MESSAGE_CHARS = 2000


def normalize_message(raw: str | None) -> str:
    """Return the trimmed form of ``raw``, capped at MAX_MESSAGE_CHARS.

    Empty / whitespace-only input returns an empty string. The caller
    treats that as a validation error — never a successful submit.
    """
    if raw is None:
        return ""
    stripped = str(raw).strip()
    return stripped[:MAX_MESSAGE_CHARS]


def has_applied(listing_id: str, applicant_user_id: str) -> bool:
    """True when ``applicant_user_id`` already has an application row
    for ``listing_id``. Any read failure is treated as "unknown — don't
    pretend they applied"; the unique index is still the authority on
    a concurrent duplicate insert.
    """
    lid = safe_uuid(listing_id)
    uid = safe_uuid(applicant_user_id)
    if not lid or not uid:
        return False
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_applications")
            .select("id")
            .eq("listing_id", lid)
            .eq("applicant_user_id", uid)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception(
            "job_applications.has_applied.read_failed listing=%s", lid
        )
        return False
    rows = getattr(result, "data", None) or []
    return bool(rows)


def create(
    *,
    listing_id: str,
    applicant_user_id: str,
    message: str,
) -> dict[str, Any] | None:
    """Insert one application row. Returns the inserted row on success,
    or ``None`` on any validation / persistence failure.

    The caller MUST already have verified:
      * the authenticated session's role is ``creator``
      * the opportunity exists and is viewable
      * the viewer is not the poster

    This function additionally rejects:
      * blank or oversized messages
      * duplicate applications (fast path via ``has_applied``)
      * uuid-shaped input that doesn't parse

    A race between ``has_applied`` and this insert is caught by the
    DB unique constraint, which raises ``PostgrestAPIError``; this
    function treats that the same as a non-race failure (logs and
    returns ``None``). The route re-renders with a banner either way.
    """
    lid = safe_uuid(listing_id)
    uid = safe_uuid(applicant_user_id)
    if not lid or not uid:
        return None

    clean_message = normalize_message(message)
    if not clean_message:
        return None
    if len(clean_message) > MAX_MESSAGE_CHARS:
        # normalize_message already caps at MAX_MESSAGE_CHARS; this is
        # a belt against a future refactor dropping the cap.
        return None

    # Fast-path duplicate check — makes the UX nicer and avoids a
    # noisy PostgREST error log for the common double-submit case.
    if has_applied(lid, uid):
        return None

    body = {
        "listing_id": lid,
        "applicant_user_id": uid,
        "message": clean_message,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_applications")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception(
            "job_applications.create.write_failed listing=%s", lid
        )
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


# Page size for the counting scan. PostgREST caps a single response at
# 1000 rows by default, so a larger scan must page rather than silently
# truncating — a truncated count would be a wrong (faked) number.
_COUNT_PAGE_SIZE = 1000
# Hard ceiling on pages per call (20k application rows). Past this we
# report "unknown" (None) instead of a partial number.
_COUNT_MAX_PAGES = 20
# Keep each ``in_`` filter comfortably inside URL-length limits.
_COUNT_ID_CHUNK = 50


def list_for_applicant(
    applicant_user_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """The calling creator's OWN applications, newest first.

    Returns only ``listing_id`` and ``created_at`` — the application
    ``message`` is deliberately never selected, so it cannot leak into a
    list surface. ``applicant_user_id`` must come from the authenticated
    session at the route layer; this function filters on it directly.

    Any read failure returns ``[]`` (logged); callers render the normal
    empty state rather than a 500.
    """
    uid = safe_uuid(applicant_user_id)
    if not uid:
        return []
    capped = max(1, min(int(limit), 200))
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_applications")
            .select("listing_id,created_at")
            .eq("applicant_user_id", uid)
            .order("created_at", desc=True)
            .limit(capped)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_applications.list_for_applicant.read_failed")
        return []
    return list(getattr(result, "data", None) or [])


def count_by_listing(listing_ids: list[str]) -> dict[str, int] | None:
    """Real applicant counts for the given listings.

    Returns ``{listing_id: count}`` with an explicit ``0`` for listings
    that have no applications. Returns ``None`` when the count cannot be
    determined (read failure or an absurdly large scan) — callers must
    treat that as "unknown" and omit the number rather than show 0.

    SECURITY: this function does not check ownership. The caller MUST
    pass only listing ids it has already established the authenticated
    user owns (``my_opportunities.poster_items`` derives them from
    ``jobs.list_by_poster(<session user id>)``). Only ``listing_id`` is
    selected, so no application content can leave this function.
    """
    ids = [lid for lid in (safe_uuid(i) for i in listing_ids) if lid]
    counts: dict[str, int] = dict.fromkeys(ids, 0)
    if not ids:
        return counts
    for start in range(0, len(ids), _COUNT_ID_CHUNK):
        chunk = ids[start : start + _COUNT_ID_CHUNK]
        for page in range(_COUNT_MAX_PAGES):
            lo = page * _COUNT_PAGE_SIZE
            try:
                result = (
                    supabase_client.get_service_client()
                    .table("creator_job_applications")
                    .select("listing_id")
                    .in_("listing_id", chunk)
                    .order("created_at")
                    .order("id")
                    .range(lo, lo + _COUNT_PAGE_SIZE - 1)
                    .execute()
                )
            except PostgrestAPIError:
                logger.exception("job_applications.count_by_listing.read_failed")
                return None
            rows = list(getattr(result, "data", None) or [])
            for row in rows:
                lid = str(row.get("listing_id") or "")
                if lid in counts:
                    counts[lid] += 1
            if len(rows) < _COUNT_PAGE_SIZE:
                break
        else:
            logger.warning("job_applications.count_by_listing.scan_cap_hit")
            return None
    return counts
