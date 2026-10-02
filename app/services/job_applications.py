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
  applications for a listing" helper — that's Step 5C's job and needs
  its own explicit server-side owner check.
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
