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
  only reader. Reads fall into three groups:

  - applicant-side, scoped to the caller's OWN user id
    (``has_applied``, ``list_for_applicant``);
  - bare integers (``count_by_listing``);
  - poster-side applicant review (Step 5D: ``authorize_poster``,
    ``list_for_poster``, ``get_for_poster``). These are the ONLY
    functions that can return another user's application, and each one
    re-proves ownership itself: the listing's ``poster_user_id`` must
    equal the authenticated user id passed in by the route. The list
    read never selects ``message``; only ``get_for_poster`` does, and it
    additionally pins the application to the listing in the query.

  Every other read never selects the ``message`` column.
* **Duplicate safety at two layers.** `has_applied` short-circuits the
  obvious duplicate for a clean UX; the unique index
  ``creator_job_applications_once_per_creator`` is the final guard
  against concurrent submits racing past the pre-check.
* **No raise.** Supabase failures log and return False / None so a
  single flaky write never surfaces as a 500 — the route re-renders
  the form with a banner.

Keep any further owner-side read helpers in this module rather than
exposing a generic "SELECT * FROM creator_job_applications" elsewhere.
"""

from __future__ import annotations

import logging
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import jobs, profiles

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


# --------------------------------------------------------------------------
# Step 5D: poster-side applicant review (read only)
# --------------------------------------------------------------------------

REVIEW_OK = "ok"
REVIEW_NOT_FOUND = "not_found"
REVIEW_FORBIDDEN = "forbidden"

# Applicant rows per review page. Generous for one listing; the count shown
# on My opportunities is the authoritative total.
_REVIEW_MAX_ROWS = 200
# Same URL-length reasoning as ``_COUNT_ID_CHUNK``: ``in_`` filters stay short.
_PROFILE_ID_CHUNK = 50


def _owns(listing: dict[str, Any], user_id: str) -> bool:
    """True only when ``listing.poster_user_id`` is exactly ``user_id``.

    Both sides must parse as UUIDs; anything malformed or missing is
    "not the owner", so a bad value can never accidentally match another
    bad value.
    """
    owner = safe_uuid(str(listing.get("poster_user_id") or ""))
    viewer = safe_uuid(str(user_id or ""))
    return bool(owner and viewer and owner == viewer)


def authorize_poster(
    listing_id: str, viewer_user_id: str
) -> tuple[str, dict[str, Any] | None]:
    """Decide whether ``viewer_user_id`` may review applicants for a listing.

    Returns ``(REVIEW_OK, listing)`` only when the authenticated viewer is
    the listing's poster. Otherwise ``(REVIEW_NOT_FOUND | REVIEW_FORBIDDEN,
    None)`` -- the listing row is never handed back to a non-owner.

    * malformed ids, a missing listing, or a listing the viewer cannot see
      at all (taken down, or not publicly viewable and not theirs) ->
      ``REVIEW_NOT_FOUND`` (same 404 the opportunity detail page gives);
    * a visible listing posted by someone else -> ``REVIEW_FORBIDDEN``;
    * a CLOSED listing the viewer posted is still reviewable: they posted
      it, and closing it should not strand the applications it received.

    ``viewer_user_id`` must come from the authenticated session, never a
    query string or form field.
    """
    lid = safe_uuid(listing_id)
    uid = safe_uuid(viewer_user_id)
    if not lid or not uid:
        return REVIEW_NOT_FOUND, None
    listing = jobs.get(lid)
    if not isinstance(listing, dict) or not jobs.can_view_detail(listing, uid):
        return REVIEW_NOT_FOUND, None
    if not _owns(listing, uid):
        return REVIEW_FORBIDDEN, None
    return REVIEW_OK, listing


def list_for_poster(
    listing: dict[str, Any],
    poster_user_id: str,
    *,
    limit: int = _REVIEW_MAX_ROWS,
) -> list[dict[str, Any]] | None:
    """Applications to ``listing``, newest first, for the listing's poster.

    Returns ``None`` when ownership is not proven or the read fails -- the
    caller must NOT render that as "no applicants" (``[]`` is the only
    value that means zero). Rows carry ONLY ``id``, ``applicant_user_id``
    and ``created_at``: the ``message`` column is neither selected nor
    copied, so it cannot reach a list surface.
    """
    lid = safe_uuid(str(listing.get("id") or ""))
    if not lid or not _owns(listing, poster_user_id):
        return None
    capped = max(1, min(int(limit), _REVIEW_MAX_ROWS))
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_applications")
            .select("id,applicant_user_id,created_at")
            .eq("listing_id", lid)
            .order("created_at", desc=True)
            .order("id")
            .limit(capped)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_applications.list_for_poster.read_failed listing=%s", lid)
        return None
    return [
        {
            "id": str(row.get("id") or ""),
            "applicant_user_id": str(row.get("applicant_user_id") or ""),
            "created_at": row.get("created_at"),
        }
        for row in (getattr(result, "data", None) or [])
    ]


def get_for_poster(
    listing: dict[str, Any],
    application_id: str,
    poster_user_id: str,
) -> dict[str, Any] | None:
    """One application (including its ``message``) for the listing's poster.

    The authorization chain is proven here, not assumed by the caller:

      application -> belongs to ``listing`` (the query filters on BOTH the
                     application id and the listing id, and the returned
                     ``listing_id`` is re-checked)
      listing     -> was posted by ``poster_user_id`` (``_owns``)

    If any link fails -- malformed id, someone else's listing, an
    application id belonging to a different listing, a missing row, a read
    failure -- this returns ``None`` and no message leaves the function.
    The applicant is whatever the stored row says; there is no parameter
    to choose one.
    """
    lid = safe_uuid(str(listing.get("id") or ""))
    aid = safe_uuid(application_id)
    if not lid or not aid or not _owns(listing, poster_user_id):
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_job_applications")
            .select("id,listing_id,applicant_user_id,message,created_at")
            .eq("id", aid)
            .eq("listing_id", lid)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("job_applications.get_for_poster.read_failed listing=%s", lid)
        return None
    rows = getattr(result, "data", None) or []
    if not rows:
        return None
    row = rows[0]
    # Belt and braces: never trust the filter alone to have pinned the row.
    if safe_uuid(str(row.get("listing_id") or "")) != lid:
        return None
    if safe_uuid(str(row.get("id") or "")) != aid:
        return None
    return {
        "id": aid,
        "listing_id": lid,
        "applicant_user_id": str(row.get("applicant_user_id") or ""),
        "message": str(row.get("message") or ""),
        "created_at": row.get("created_at"),
    }


def applicant_identity(
    profile: dict[str, Any] | None, user_id: str
) -> dict[str, Any]:
    """Display identity for one applicant from their EXISTING profile.

    ``profile`` is the public projection (``profiles.public_creator``), so
    nothing private can be rendered. Only fields that already exist are
    used: ``full_name``, ``instagram_handle``, ``profile_photo_url``.
    Nothing is invented -- a missing profile yields the neutral name
    "creator", no descriptor and no image.
    """
    p = profile or {}
    full_name = str(p.get("full_name") or "").strip()
    handle = str(p.get("instagram_handle") or "").strip().lstrip("@")
    name = full_name or (f"@{handle}" if handle else "creator")
    return {
        "user_id": user_id,
        "name": name,
        # Handle is the descriptor only when it adds something the name
        # does not already say.
        "descriptor": f"@{handle}" if (handle and full_name) else "",
        "image_url": str(p.get("profile_photo_url") or "").strip(),
        "initial": (name.lstrip("@")[:1] or "?").upper(),
        "found": profile is not None,
        "onboarded": bool(p.get("onboarding_completed_at")),
    }


def attach_applicants(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return ``rows`` with an ``applicant`` identity attached to each.

    Identity is resolved from each row's STORED ``applicant_user_id`` via
    the existing bulk profile lookup (one query per 50 ids), never from
    anything the request supplied. An applicant whose profile can no
    longer be read still gets a row with the neutral identity.
    """
    ids = sorted({r["applicant_user_id"] for r in rows if r.get("applicant_user_id")})
    found: dict[str, dict[str, Any]] = {}
    for start in range(0, len(ids), _PROFILE_ID_CHUNK):
        found.update(profiles.get_creators_by_ids(ids[start : start + _PROFILE_ID_CHUNK]))
    return [
        {
            **row,
            "applicant": applicant_identity(
                found.get(str(row.get("applicant_user_id") or "")),
                str(row.get("applicant_user_id") or ""),
            ),
        }
        for row in rows
    ]
