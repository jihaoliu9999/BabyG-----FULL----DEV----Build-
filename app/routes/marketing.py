"""Public marketing pages: landing and account-type selection."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.core.security import read_session
from app.core.templating import templates

router = APIRouter(tags=["marketing"])

VALID_ROLES = {"creator", "brand", "operator"}


@router.get("/", response_class=HTMLResponse)
async def landing(request: Request):
    # Signed-in users skip the marketing page and go straight to their dashboard.
    session = read_session(request)
    if session is not None:
        return RedirectResponse(_dashboard_path(session["role"]), status_code=302)
    return templates.TemplateResponse(request, "marketing/landing.html", {})


@router.get("/get-started", response_class=HTMLResponse)
async def get_started(request: Request, role: str | None = None):
    # If a role is specified in the query (e.g. from a footer link on the
    # landing page), shortcut directly into the login form. Otherwise show
    # the role cards.
    if role and role in VALID_ROLES:
        return RedirectResponse(f"/auth/login?role={role}", status_code=302)
    return templates.TemplateResponse(request, "marketing/get_started.html", {})


def _dashboard_path(role: str) -> str:
    if role == "creator":
        return "/creator"
    if role == "brand":
        return "/brand"
    if role == "operator":
        return "/operator"
    return "/"
