"""Creator-posted opportunities — the /creator/opportunities/new form
that lets creators + brands compose a job listing that shows up as an
"opportunity" card in Discover.

Backed by ``creator_job_listings`` (see migrations/0002_schema.sql).
The row shape matches the schema: title, description, listing_type,
compensation_text, target_niches[], deadline.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import jobs

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/creator/opportunities", tags=["creator", "opportunities"])


# Human-friendly labels for the three kinds we surface in the form.
# creator_job_listings supports {'collab','ugc_gig','hiring','brand_deal'};
# we alias 'ugc_gig' as 'ugc' for the form so the label reads cleanly.
KIND_CHOICES: list[dict[str, str]] = [
    {"value": "ugc_gig", "label": "ugc brief", "hint": "paid content"},
    {"value": "collab", "label": "collab", "hint": "trade or barter"},
    {"value": "hiring", "label": "hiring", "hint": "someone to work with"},
    {"value": "brand_deal", "label": "brand deal", "hint": "sponsored work"},
]
VALID_KINDS = {c["value"] for c in KIND_CHOICES}


@router.get("/new", response_class=HTMLResponse)
async def new_opportunity_page(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Render the post-opportunity form."""
    return templates.TemplateResponse(
        request,
        "creator/opportunity_new.html",
        {
            "kind_choices": KIND_CHOICES,
            "error": None,
            "values": {},
        },
    )


@router.post("/new")
async def new_opportunity_submit(
    request: Request,
    title: str = Form(...),
    description: str = Form(...),
    listing_type: str = Form(...),
    compensation_text: str = Form(""),
    target_niches: str = Form(""),
    deadline: str = Form(""),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Validate and insert the listing, then redirect to Discover's
    Opportunities tab so the poster sees their new card lands.
    """
    title_clean = (title or "").strip()[:120]
    description_clean = (description or "").strip()[:2000]
    listing_type_clean = (listing_type or "").strip().lower()
    compensation_clean = (compensation_text or "").strip()[:120] or None

    niches = [
        n.strip().lower()[:40]
        for n in (target_niches or "").split(",")
        if n.strip()
    ][:6]

    deadline_iso: str | None = None
    raw_deadline = (deadline or "").strip()
    if raw_deadline:
        try:
            # HTML date input gives YYYY-MM-DD; store as end-of-day UTC.
            parsed = datetime.strptime(raw_deadline, "%Y-%m-%d")
            deadline_iso = parsed.strftime("%Y-%m-%dT23:59:59+00:00")
        except ValueError:
            deadline_iso = None

    error: str | None = None
    if not title_clean:
        error = "give the opportunity a title."
    elif not description_clean:
        error = "add a short description so people know what you need."
    elif listing_type_clean not in VALID_KINDS:
        error = "pick a kind: ugc brief, collab, hiring, or brand deal."

    if error is not None:
        return templates.TemplateResponse(
            request,
            "creator/opportunity_new.html",
            {
                "kind_choices": KIND_CHOICES,
                "error": error,
                "values": {
                    "title": title_clean,
                    "description": description_clean,
                    "listing_type": listing_type_clean,
                    "compensation_text": compensation_clean or "",
                    "target_niches": ", ".join(niches),
                    "deadline": raw_deadline,
                },
            },
            status_code=400,
        )

    payload: dict[str, Any] = {
        "title": title_clean,
        "description": description_clean,
        "listing_type": listing_type_clean,
        "target_niches": niches,
    }
    if compensation_clean:
        payload["compensation_text"] = compensation_clean
    if deadline_iso:
        payload["deadline"] = deadline_iso

    created_id = jobs.create(poster_id=session["user_id"], payload=payload)
    if not created_id:
        # Re-render the form with the banner + all their input preserved
        # so retry doesn't cost them any typing. 200 (not 5xx) so the
        # app-wide error middleware doesn't swap in the generic error page.
        return templates.TemplateResponse(
            request,
            "creator/opportunity_new.html",
            {
                "kind_choices": KIND_CHOICES,
                "error": "couldn't save the opportunity. try again in a moment.",
                "values": {
                    "title": title_clean,
                    "description": description_clean,
                    "listing_type": listing_type_clean,
                    "compensation_text": compensation_clean or "",
                    "target_niches": ", ".join(niches),
                    "deadline": raw_deadline,
                },
            },
        )

    return RedirectResponse(
        "/creator/discover?kind=opportunity&posted=1", status_code=303
    )
