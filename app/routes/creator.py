"""Creator dashboard — Daily Hot Drops feed.

Today this is one route (`/creator`) plus a feedback endpoint. As Phase 1
fills in, we'll add booking, calendar, network, and the chat surface, all
under this prefix.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import intel, profiles

router = APIRouter(tags=["creator"])

CATEGORY_LABELS = {
    "venue": "Venue",
    "trend": "Trend",
    "brand": "Brand",
    "collab": "Collab",
    "alert": "Alert",
}


@router.get("/creator", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    category: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    posts = intel.feed_for_creator(
        niches=profile.get("niches") or [],
        tier=profile.get("tier") or "basic",
        category=category if category in intel.CATEGORIES else None,
    )
    post_ids = [str(p["id"]) for p in posts]
    feedback_map = intel.feedback_for_user(session["user_id"], post_ids)

    return templates.TemplateResponse(
        request,
        "creator/dashboard.html",
        {
            "profile": profile,
            "posts": posts,
            "feedback_map": feedback_map,
            "categories": list(CATEGORY_LABELS.items()),
            "active_category": category if category in intel.CATEGORIES else None,
        },
    )


@router.post("/creator/intel/{post_id}/feedback")
async def submit_feedback(
    post_id: str,
    signal: str = Form(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if not intel.record_feedback(
        user_id=session["user_id"], intel_post_id=post_id, signal=signal
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return RedirectResponse("/creator", status_code=303)
