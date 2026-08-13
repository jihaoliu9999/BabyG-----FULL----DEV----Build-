"""Public legal / compliance pages.

These render plain HTML and accept both anonymous and signed-in users.
No CSRF, no role gating, no DB calls — purely informational. The
`last_updated` value is centralized here so all four pages stay in sync.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.core.templating import templates

router = APIRouter(tags=["legal"])

# Update this when any legal page's wording changes meaningfully. All four
# pages render the same date so visitors can tell the policy set is current.
LAST_UPDATED = "August 2026"

# Single contact address for every legal page. Centralized so a future
# rename only touches one line.
CONTACT_EMAIL = "babygaiagent@gmail.com"


def _ctx() -> dict[str, str]:
    return {"last_updated": LAST_UPDATED, "contact_email": CONTACT_EMAIL}


@router.get("/privacy", response_class=HTMLResponse)
async def privacy(request: Request):
    return templates.TemplateResponse(request, "legal/privacy.html", _ctx())


@router.get("/terms", response_class=HTMLResponse)
async def terms(request: Request):
    return templates.TemplateResponse(request, "legal/terms.html", _ctx())


@router.get("/accessibility", response_class=HTMLResponse)
async def accessibility(request: Request):
    return templates.TemplateResponse(
        request, "legal/accessibility.html", _ctx()
    )


@router.get("/data-deletion", response_class=HTMLResponse)
async def data_deletion(request: Request):
    return templates.TemplateResponse(
        request, "legal/data_deletion.html", _ctx()
    )


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request):
    return templates.TemplateResponse(request, "legal/about.html", _ctx())


@router.get("/contact", response_class=HTMLResponse)
async def contact(request: Request):
    return templates.TemplateResponse(request, "legal/contact.html", _ctx())
