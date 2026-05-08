"""Onboarding wizards for creators and brands.

Single-page submit per role; data goes to the matching profile row that the
auth callback pre-created. On success we set `onboarding_completed_at` and
redirect to the dashboard.

Operators have no onboarding form — they're invite-only and pre-provisioned.
`/onboarding/operator` just renders a one-time welcome card.

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
from app.services import profiles

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

BRAND_INDUSTRIES = [
    "fashion", "beauty", "food & beverage", "fitness & wellness",
    "travel & hospitality", "tech", "finance", "lifestyle",
    "home goods", "automotive", "other",
]
BRAND_SCALES = ["boutique", "growth-stage", "established", "enterprise"]
BRAND_MODELS = ["DTC", "wholesale", "subscription", "marketplace", "service"]
BRAND_POSITIONINGS = ["luxury", "premium", "mid-market", "value", "ethical/sustainable"]
BRAND_CAMPAIGN_TYPES = [
    "ugc", "paid posts", "gifting", "events", "ambassadors", "brand integrations",
]
BRAND_CREATOR_SIZES = ["nano", "micro", "mid", "macro"]
BRAND_NICHE_PREFS = CREATOR_NICHES                # same vocabulary
BRAND_BUDGET_RANGES = ["<$1k", "$1-5k", "$5-15k", "$15-50k", "$50k+"]


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
    return templates.TemplateResponse(
        request,
        "onboarding/creator.html",
        {
            "profile": profile,
            "vocab": _creator_vocab(),
            "error": None,
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


@router.get("/brand", response_class=HTMLResponse)
async def brand_form(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
):
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if profile.get("onboarding_completed_at"):
        return RedirectResponse("/brand", status_code=302)
    return templates.TemplateResponse(
        request,
        "onboarding/brand.html",
        {
            "profile": profile,
            "vocab": _brand_vocab(),
            "error": None,
        },
    )


@router.post("/brand")
async def brand_submit(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
):
    form = await request.form()
    payload, error = _validate_brand(form)
    if error:
        return _brand_error(request, form, error)

    if not profiles.complete_brand_onboarding(session["user_id"], payload):
        return _brand_error(request, form, "We couldn't save your profile. Try again.")
    return RedirectResponse("/brand", status_code=303)


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


def _validate_brand(form) -> tuple[dict[str, Any], str | None]:
    company = _str(form.get("company_name"), 160)
    website = _str(form.get("brand_website"), 240)
    contact_name = _str(form.get("contact_full_name"), 120)
    contact_title = _str(form.get("contact_title"), 120)
    description = _str(form.get("product_description"), 800)

    if not company:
        return {}, "Please enter your company name."
    if not website:
        return {}, "Please enter your brand website."
    if not contact_name:
        return {}, "Please enter your name."

    payload: dict[str, Any] = {
        "company_name": company,
        "brand_website": website,
        "contact_full_name": contact_name,
        "contact_title": contact_title or None,
        "product_description": description or None,
        "industry": _enum(form.get("industry"), BRAND_INDUSTRIES),
        "scale_descriptor": _enum(form.get("scale_descriptor"), BRAND_SCALES),
        "model_descriptor": _enum(form.get("model_descriptor"), BRAND_MODELS),
        "positioning_descriptor": _enum(
            form.get("positioning_descriptor"), BRAND_POSITIONINGS
        ),
        "campaign_types": _multi(form.getlist("campaign_types"), BRAND_CAMPAIGN_TYPES),
        "creator_size_preferences": _multi(
            form.getlist("creator_size_preferences"), BRAND_CREATOR_SIZES
        ),
        "niche_preferences": _multi(form.getlist("niche_preferences"), BRAND_NICHE_PREFS),
        "budget_range": _enum(form.get("budget_range"), BRAND_BUDGET_RANGES),
    }

    if not payload["campaign_types"]:
        return {}, "Pick at least one campaign type you're interested in."
    if not payload["creator_size_preferences"]:
        return {}, "Pick at least one creator size you'd like to work with."

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


def _brand_vocab() -> dict[str, list[str]]:
    return {
        "industries": BRAND_INDUSTRIES,
        "scales": BRAND_SCALES,
        "models": BRAND_MODELS,
        "positionings": BRAND_POSITIONINGS,
        "campaign_types": BRAND_CAMPAIGN_TYPES,
        "creator_sizes": BRAND_CREATOR_SIZES,
        "niche_prefs": BRAND_NICHE_PREFS,
        "budgets": BRAND_BUDGET_RANGES,
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


def _brand_error(request: Request, form, message: str) -> Response:
    return templates.TemplateResponse(
        request,
        "onboarding/brand.html",
        {
            "profile": _form_to_profile(form),
            "vocab": _brand_vocab(),
            "error": message,
        },
        status_code=400,
    )


def _form_to_profile(form) -> dict[str, Any]:
    """Reconstruct a profile-shaped dict from form data so the template can
    re-render the user's selections after a validation error."""
    out: dict[str, Any] = {}
    multi_keys = {
        "niches", "content_formats", "hard_limits", "campaign_types",
        "creator_size_preferences", "niche_preferences",
    }
    for key in form:
        if key in multi_keys:
            out[key] = list(form.getlist(key))
        else:
            out[key] = form.get(key)
    return out
