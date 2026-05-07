"""Brand console — /brand and creator discovery.

Pre-verification: shows a "Verification pending" pane.
Post-verification: lists matched creators, lets a brand view a creator
profile and send outreach. Outreach in this step writes a notification
on the creator's account; the actual DM thread arrives in Step 7.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import brands, creators, notifications

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brand", tags=["brand"])


@router.get("", response_class=HTMLResponse)
async def console(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    profile = brands.get_by_user_id(session["user_id"])
    if profile is None or not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    if not profile.get("is_verified"):
        return templates.TemplateResponse(
            request,
            "brand/console_pending.html",
            {"profile": profile},
        )

    matches = creators.list_for_brand_match(
        niche_preferences=profile.get("niche_preferences") or [],
        creator_size_preferences=profile.get("creator_size_preferences") or [],
    )
    return templates.TemplateResponse(
        request,
        "brand/console.html",
        {"profile": profile, "matches": matches[:24]},
    )


@router.get("/creators", response_class=HTMLResponse)
async def creators_list(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile
    matches = creators.list_for_brand_match(
        niche_preferences=profile.get("niche_preferences") or [],
        creator_size_preferences=profile.get("creator_size_preferences") or [],
    )
    return templates.TemplateResponse(
        request,
        "brand/creators_list.html",
        {"profile": profile, "matches": matches},
    )


@router.get("/creators/{creator_user_id}", response_class=HTMLResponse)
async def creator_detail(
    creator_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile

    creator = creators.get_for_view(creator_user_id)
    if creator is None or not creator.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    return templates.TemplateResponse(
        request,
        "brand/creator_detail.html",
        {
            "profile": profile,
            "creator": creator,
            "error": None,
            "pitch": "",
        },
    )


@router.post("/creators/{creator_user_id}/outreach")
async def creator_outreach(
    creator_user_id: str,
    request: Request,
    pitch: str = Form(...),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile

    pitch = (pitch or "").strip()[:500]
    if len(pitch) < 20:
        return _outreach_error(
            request,
            creator_user_id=creator_user_id,
            profile=profile,
            pitch=pitch,
            message="Please write at least a couple of sentences (20+ characters).",
        )

    creator = creators.get_for_view(creator_user_id)
    if creator is None or not creator.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    title = f"{profile.get('company_name') or 'A brand'} wants to work with you."
    ok = notifications.create(
        user_id=creator_user_id,
        kind="collab_match",
        title=title,
        body=pitch,
        link_path=f"/creator/brands/{session['user_id']}",
    )
    if not ok:
        return _outreach_error(
            request,
            creator_user_id=creator_user_id,
            profile=profile,
            pitch=pitch,
            message="Couldn't send. Try again.",
        )
    return RedirectResponse(
        f"/brand/creators/{creator_user_id}?sent=1", status_code=303
    )


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _verified_brand_or_redirect(user_id: str) -> dict[str, Any] | RedirectResponse:
    profile = brands.get_by_user_id(user_id)
    if profile is None or not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    if not profile.get("is_verified"):
        return RedirectResponse("/brand", status_code=302)
    return profile


def _outreach_error(
    request: Request,
    *,
    creator_user_id: str,
    profile: dict[str, Any],
    pitch: str,
    message: str,
) -> Response:
    creator = creators.get_for_view(creator_user_id)
    return templates.TemplateResponse(
        request,
        "brand/creator_detail.html",
        {
            "profile": profile,
            "creator": creator or {"user_id": creator_user_id},
            "error": message,
            "pitch": pitch,
        },
        status_code=400,
    )


