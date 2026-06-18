"""Brand dashboard foundation.

P1 restores the brand role path without re-enabling the larger brand
outreach, DM, or matching product. A signed-in brand can finish onboarding
and land on a quiet shell that proves routing/profile storage are alive.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import profiles

router = APIRouter(prefix="/brand", tags=["brand"])


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
):
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    return templates.TemplateResponse(
        request,
        "brand/dashboard.html",
        {"profile": profile},
    )
