"""Onboarding wizard for creators.

Single-page submit; data goes to the creator_profile row that the auth
callback pre-created. On success we set `onboarding_completed_at` and
redirect to the dashboard.

Operators have no onboarding form — they're invite-only and pre-provisioned.
`/onboarding/operator` just renders a one-time welcome card.

Brand-side onboarding is deferred to v1.5 (preserved on the
brand-side-v1.5 branch).

Validation is intentionally light: required fields are checked, free text is
length-capped, and unknown values for closed-set fields are dropped (we don't
want to crash the wizard on a stale enum from a cached page).
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.integrations import google_calendar
from app.services import oauth_connections, profiles

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])


# -----------------------------------------------------------------------------
# Closed-set vocabularies. The form template renders these directly; the
# POST handler whitelists submitted values against them.
# -----------------------------------------------------------------------------

CREATOR_NICHES = [
    "food", "fashion", "beauty", "fitness", "wellness", "travel",
    "lifestyle", "nightlife", "music", "art", "real-estate", "tech",
]
CREATOR_FORMATS = [
    "reels", "carousels", "stories", "static", "long-form", "ugc",
]
CREATOR_NEIGHBORHOODS = [
    "Wynwood", "Brickell", "Miami Beach", "Coconut Grove", "Coral Gables",
    "Edgewater", "Downtown", "Little Havana", "Design District", "Aventura",
    "Other",
]
CREATOR_FOLLOWER_RANGES = ["<10k", "10-50k", "50-100k", "100-500k", "500k+"]
CREATOR_ENGAGEMENT_RANGES = ["<2%", "2-4%", "4-7%", "7%+"]
CREATOR_TENURES = ["<6mo", "6-12mo", "1-2y", "2-5y", "5+y"]
CREATOR_PLATFORMS = ["Instagram", "TikTok", "YouTube"]
CREATOR_HARD_LIMITS = [
    "no alcohol", "no nicotine", "no fast fashion", "no crypto",
    "no MLM", "no gambling", "no political",
]
CREATOR_TIERS = ["basic", "pro", "vip"]


# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------


@router.get("/creator", response_class=HTMLResponse)
async def creator_form(
    request: Request, session: SessionPayload = Depends(require_role("creator"))
):
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if profile.get("onboarding_completed_at"):
        return RedirectResponse("/creator", status_code=302)

    # Step 4 (integrations) renders the same partial used on
    # profile/settings. Google Calendar is the only real connector
    # today; the rest are coming-soon and don't read these flags.
    # Wrapped defensively so a missing-Supabase test environment
    # doesn't blow up the wizard render — the worst case is the
    # Google connect card showing "not configured".
    try:
        google_configured = google_calendar.is_configured()
    except Exception:
        logger.exception("onboarding: google_calendar.is_configured failed")
        google_configured = False
    try:
        google_connected = (
            oauth_connections.get_google_connection(session["user_id"]) is not None
        )
    except Exception:
        logger.exception("onboarding: get_google_connection failed")
        google_connected = False

    return templates.TemplateResponse(
        request,
        "onboarding/creator.html",
        {
            "profile": profile,
            "vocab": _creator_vocab(),
            "error": None,
            "google_configured": google_configured,
            "google_connected": google_connected,
        },
    )


@router.post("/creator")
async def creator_submit(
    request: Request, session: SessionPayload = Depends(require_role("creator"))
):
    form = await request.form()
    payload, error = _validate_creator(form)
    if error:
        return _creator_error(request, form, error)

    try:
        ok = profiles.complete_creator_onboarding(session["user_id"], payload)
    except profiles.HandleAlreadyTakenError:
        return _creator_error(
            request, form,
            "That Instagram handle is already in use on babyg. Pick a different one.",
        )
    if not ok:
        return _creator_error(
            request, form, "We couldn't save your profile. Try again."
        )
    return RedirectResponse("/creator", status_code=303)


@router.get("/operator", response_class=HTMLResponse)
async def operator_welcome(
    request: Request, session: SessionPayload = Depends(require_role("operator"))
):
    return templates.TemplateResponse(request, "onboarding/operator.html", {})


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def _validate_creator(form) -> tuple[dict[str, Any], str | None]:
    full_name = _str(form.get("full_name"), 120)
    handle = _str(form.get("instagram_handle"), 64)
    if handle.startswith("@"):
        handle = handle[1:]
    bio = _str(form.get("bio"), 600)
    neighborhood = _str(form.get("neighborhood"), 64)

    if not full_name:
        return {}, "Please enter your full name."
    if not handle:
        return {}, "Please enter your Instagram handle."
    if neighborhood and neighborhood not in CREATOR_NEIGHBORHOODS:
        return {}, "Pick a neighborhood from the list (or leave it blank)."

    payload: dict[str, Any] = {
        "full_name": full_name,
        "instagram_handle": handle.lower(),
        "neighborhood": neighborhood or None,
        "bio": bio or None,
        "niches": _multi(form.getlist("niches"), CREATOR_NICHES),
        "content_formats": _multi(form.getlist("content_formats"), CREATOR_FORMATS),
        "lifestyle_tags": [],   # reserved for later; not collected at v1
        "brand_preferences": [],
        "hard_limits": _multi(form.getlist("hard_limits"), CREATOR_HARD_LIMITS),
        "follower_range": _enum(form.get("follower_range"), CREATOR_FOLLOWER_RANGES),
        "engagement_range": _enum(form.get("engagement_range"), CREATOR_ENGAGEMENT_RANGES),
        "creator_tenure": _enum(form.get("creator_tenure"), CREATOR_TENURES),
        "primary_platform": _enum(form.get("primary_platform"), CREATOR_PLATFORMS),
        "tier": _enum(form.get("tier"), CREATOR_TIERS) or "basic",
    }

    if not payload["niches"]:
        return {}, "Pick at least one niche."
    if not payload["content_formats"]:
        return {}, "Pick at least one content format."

    return payload, None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _str(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _enum(value: Any, allowed: list[str]) -> str | None:
    if value is None:
        return None
    v = str(value).strip()
    return v if v in allowed else None


def _multi(values: list[Any], allowed: list[str]) -> list[str]:
    seen: list[str] = []
    allowed_set = set(allowed)
    for v in values:
        s = str(v).strip()
        if s in allowed_set and s not in seen:
            seen.append(s)
    return seen


def _creator_vocab() -> dict[str, list[str]]:
    return {
        "niches": CREATOR_NICHES,
        "formats": CREATOR_FORMATS,
        "neighborhoods": CREATOR_NEIGHBORHOODS,
        "follower_ranges": CREATOR_FOLLOWER_RANGES,
        "engagement_ranges": CREATOR_ENGAGEMENT_RANGES,
        "tenures": CREATOR_TENURES,
        "platforms": CREATOR_PLATFORMS,
        "hard_limits": CREATOR_HARD_LIMITS,
        "tiers": CREATOR_TIERS,
    }


def _creator_error(request: Request, form, message: str) -> Response:
    return templates.TemplateResponse(
        request,
        "onboarding/creator.html",
        {
            "profile": _form_to_profile(form),
            "vocab": _creator_vocab(),
            "error": message,
        },
        status_code=400,
    )


def _form_to_profile(form) -> dict[str, Any]:
    """Reconstruct a profile-shaped dict from form data so the template can
    re-render the user's selections after a validation error."""
    out: dict[str, Any] = {}
    multi_keys = {"niches", "content_formats", "hard_limits"}
    for key in form:
        if key in multi_keys:
            out[key] = list(form.getlist(key))
        else:
            out[key] = form.get(key)
    return out
