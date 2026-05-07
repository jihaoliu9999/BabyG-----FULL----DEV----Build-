"""Shared abuse-report endpoint.

Any signed-in non-operator user can submit a report. The form is
embedded in DM thread pages and the read-only profile pages on each
side; on submit we record the report and bounce back to wherever the
user came from (with a ?reported=1 hint for the destination template).
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, HTTPException, status
from fastapi.responses import RedirectResponse, Response

from app.core.security import SessionPayload
from app.deps import current_user
from app.services import abuse

logger = logging.getLogger(__name__)

router = APIRouter(tags=["moderation"])

# Only the four target types from the schema CHECK constraint.
ALLOWED_TARGETS = abuse.TARGET_TYPES


@router.post("/report")
async def report(
    target_type: str = Form(...),
    target_id: str = Form(...),
    reason: str = Form(...),
    return_to: str = Form("/"),
    session: SessionPayload = Depends(current_user),
) -> Response:
    if session["role"] == "operator":
        # Operators don't file reports; they review them.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if target_type not in ALLOWED_TARGETS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    ok = abuse.create_report(
        reporter_id=session["user_id"],
        target_type=target_type,
        target_id=target_id,
        reason=reason,
    )
    safe_return = _safe_return(return_to)
    sep = "&" if "?" in safe_return else "?"
    suffix = f"{sep}reported={'1' if ok else 'fail'}"
    return RedirectResponse(safe_return + suffix, status_code=303)


def _safe_return(value: str) -> str:
    """Restrict redirects to same-origin paths so the form can't be used
    as an open redirect."""
    if not value or not value.startswith("/"):
        return "/"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return "/"
    # Strip any pre-existing ?reported=... so we don't stack hints.
    base = parsed.path
    if parsed.query:
        kept = "&".join(
            p for p in parsed.query.split("&")
            if p and not p.startswith("reported=")
        )
        if kept:
            base = f"{base}?{kept}"
    return base
