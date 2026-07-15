"""Onboarding wizards for creators and brands.

Single-page submit; data goes to the matching profile row that the auth
callback pre-created. On success we set `onboarding_completed_at` and
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
from app.core.url_guard import http_url_or_none
from app.deps import require_role
from app.integrations import google_calendar, instagram_meta
from app.services import locations, oauth_connections, profiles

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
CREATOR_FORMATS = ["reels", "stories", "long-form"]
CREATOR_PLATFORMS = ["Instagram", "TikTok"]
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
BRAND_NICHE_PREFS = CREATOR_NICHES
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

    # Step 5 (integrations) keeps the existing OAuth buttons, but the
    # onboarding page itself stays simple. Google Calendar and Gmail
    # share the Google OAuth connection.
    # Wrapped defensively so a missing-Supabase test environment
    # doesn't blow up the wizard render — the worst case is the
    # Google connect card showing "not configured".
    try:
        google_configured = google_calendar.is_configured()
    except Exception:
        logger.exception("onboarding: google_calendar.is_configured failed")
        google_configured = False
    try:
        google_connection = oauth_connections.get_google_connection(session["user_id"])
        google_calendar_connected = oauth_connections.google_calendar_connected(
            google_connection
        )
        google_gmail_connected = oauth_connections.google_gmail_connected(
            google_connection
        )
    except Exception:
        logger.exception("onboarding: get_google_connection failed")
        google_calendar_connected = False
        google_gmail_connected = False

    # Instagram is independent of Google. Same defensive wrap so a
    # missing-Supabase test env can't 500 the wizard.
    try:
        instagram_configured = instagram_meta.is_configured()
    except Exception:
        logger.exception("onboarding: instagram_meta.is_configured failed")
        instagram_configured = False
    # Default to None so the template-context dict construction below
    # can always reference `instagram_connection` — _instagram_username_
    # _from_connection() tolerates None and returns "".
    instagram_connection: dict[str, Any] | None = None
    try:
        instagram_connection = oauth_connections.get_instagram_connection(
            session["user_id"]
        )
        instagram_connected = instagram_connection is not None
    except Exception:
        logger.exception("onboarding: get_instagram_connection failed")
        instagram_connected = False

    return templates.TemplateResponse(
        request,
        "onboarding/creator.html",
        {
            "profile": profile,
            "vocab": _creator_vocab(),
            "error": None,
            "google_configured": google_configured,
            "google_calendar_connected": google_calendar_connected,
            "google_gmail_connected": google_gmail_connected,
            "instagram_configured": instagram_configured,
            "instagram_connected": instagram_connected,
            "instagram_suggested_handle": _instagram_username_from_connection(
                instagram_connection
            ),
        },
    )


@router.post("/creator")
async def creator_submit(
    request: Request, session: SessionPayload = Depends(require_role("creator"))
):
    form = await request.form()
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    try:
        instagram_connection = oauth_connections.get_instagram_connection(
            session["user_id"]
        )
    except Exception:
        logger.exception("onboarding: get_instagram_connection failed")
        instagram_connection = None
    payload, error = _validate_creator(
        form,
        existing_profile=profile,
        instagram_connection=instagram_connection,
    )
    if error:
        return _creator_error(request, form, error)

    try:
        ok = profiles.complete_creator_onboarding(session["user_id"], payload)
    except profiles.HandleAlreadyTakenError:
        return _creator_error(
            request, form,
            "that instagram account is already in use on babyg. connect a different account.",
        )
    if not ok:
        return _creator_error(
            request, form, "we couldn't save your profile. try again."
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
        {"profile": profile, "vocab": _brand_vocab(), "error": None},
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
        return _brand_error(request, form, "we couldn't save your profile. try again.")
    return RedirectResponse("/brand", status_code=303)


@router.get("/operator", response_class=HTMLResponse)
async def operator_welcome(
    request: Request, session: SessionPayload = Depends(require_role("operator"))
):
    return templates.TemplateResponse(request, "onboarding/operator.html", {})


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def _validate_creator(
    form,
    *,
    existing_profile: dict[str, Any] | None = None,
    instagram_connection: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None]:
    full_name = _str(form.get("full_name"), 120)
    bio = _str(form.get("bio"), 600)
    location_payload, location_error = locations.profile_location_payload(form)
    if location_error:
        return {}, location_error
    handle = _str(
        (existing_profile or {}).get("instagram_handle")
        or _instagram_username_from_connection(instagram_connection),
        64,
    )
    if handle.startswith("@"):
        handle = handle[1:]

    if not full_name:
        return {}, "please enter your full name."

    niches = _multi(form.getlist("niches"), CREATOR_NICHES)
    custom_niche = _custom_niche(form.get("niche_other"))
    if "__other__" in [str(v).strip() for v in form.getlist("niches")]:
        if custom_niche:
            if custom_niche not in niches:
                niches.append(custom_niche)
        else:
            return {}, "name your other niche or choose one from the list."

    payload: dict[str, Any] = {
        "full_name": full_name,
        "instagram_handle": handle.lower() or None,
        "bio": bio or None,
        "niches": niches,
        "content_formats": _multi(form.getlist("content_formats"), CREATOR_FORMATS),
        "lifestyle_tags": [],   # reserved for later; not collected at v1
        "brand_preferences": [],
        "hard_limits": _multi(form.getlist("hard_limits"), CREATOR_HARD_LIMITS),
        # These legacy manual stats questions are intentionally no
        # longer collected. Platform stats should come from the
        # connected account sync instead.
        "follower_range": None,
        "engagement_range": None,
        "creator_tenure": None,
        "primary_platform": _enum(form.get("primary_platform"), CREATOR_PLATFORMS),
        "tier": _enum(form.get("tier"), CREATOR_TIERS) or "basic",
        **location_payload,
    }

    if not payload["niches"]:
        return {}, "pick at least one niche."
    if not payload["content_formats"]:
        return {}, "pick at least one content format."

    return payload, None


def _validate_brand(form) -> tuple[dict[str, Any], str | None]:
    company = _str(form.get("company_name"), 160)
    website = _str(form.get("brand_website"), 240)
    contact_name = _str(form.get("contact_full_name"), 120)
    contact_title = _str(form.get("contact_title"), 120)
    description = _str(form.get("product_description"), 800)

    if not company:
        return {}, "please enter your company name."
    if not website:
        return {}, "please enter your brand website."
    safe_website = http_url_or_none(website)
    if safe_website is None:
        return {}, "please enter a valid http or https url for your brand website."
    if not contact_name:
        return {}, "please enter your name."

    payload: dict[str, Any] = {
        "company_name": company,
        "brand_website": safe_website,
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
        return {}, "pick at least one campaign type."
    if not payload["creator_size_preferences"]:
        return {}, "pick at least one creator size."

    return payload, None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _str(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


def _custom_niche(value: Any) -> str:
    raw = _str(value, 40).lower()
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in {" ", "-", "&", "/"}).strip()
    return " ".join(cleaned.split())


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
        "niche_preferences": BRAND_NICHE_PREFS,
        "budget_ranges": BRAND_BUDGET_RANGES,
    }


def _creator_error(request: Request, form, message: str) -> Response:
    return templates.TemplateResponse(
        request,
        "onboarding/creator.html",
        {
            "profile": _form_to_profile(form),
            "vocab": _creator_vocab(),
            "error": message,
            "google_configured": False,
            "google_calendar_connected": False,
            "google_gmail_connected": False,
            "instagram_configured": False,
            "instagram_connected": False,
            "instagram_suggested_handle": None,
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
        "niches",
        "content_formats",
        "hard_limits",
        "campaign_types",
        "creator_size_preferences",
        "niche_preferences",
    }
    for key in form:
        if key in multi_keys:
            out[key] = list(form.getlist(key))
        else:
            out[key] = form.get(key)
    return out


def _instagram_username_from_connection(connection: dict[str, Any] | None) -> str | None:
    """Best-effort handle extraction for connection rows that include it.

    Current rows store the account id only, but this keeps onboarding ready
    for rows/tests that carry provider metadata without changing OAuth storage.
    """
    if not connection:
        return None
    candidates: list[Any] = [
        connection.get("username"),
        connection.get("provider_username"),
    ]
    for key in ("metadata", "provider_metadata", "account"):
        value = connection.get(key)
        if isinstance(value, dict):
            candidates.extend(
                [
                    value.get("username"),
                    value.get("handle"),
                    value.get("instagram_handle"),
                ]
            )
    for candidate in candidates:
        handle = _str(candidate, 64)
        if handle:
            return handle[1:] if handle.startswith("@") else handle
    return None
