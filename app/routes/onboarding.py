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
from app.integrations import google_calendar, instagram_meta
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
CREATOR_FORMATS = ["reels", "stories", "static", "long-form"]
CREATOR_NEIGHBORHOODS = [
    "Wynwood", "Brickell", "Miami Beach", "Coconut Grove", "Coral Gables",
    "Edgewater", "Downtown", "Little Havana", "Design District", "Aventura",
    "Other",
]
CREATOR_PLATFORMS = ["Instagram", "TikTok"]
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
    neighborhood = _str(form.get("neighborhood"), 64)
    handle = _str(
        (existing_profile or {}).get("instagram_handle")
        or _instagram_username_from_connection(instagram_connection),
        64,
    )
    if handle.startswith("@"):
        handle = handle[1:]

    if not full_name:
        return {}, "please enter your full name."
    if neighborhood and neighborhood not in CREATOR_NEIGHBORHOODS:
        return {}, "pick a neighborhood from the list or leave it blank."

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
        "neighborhood": neighborhood or None,
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
    }

    if not payload["niches"]:
        return {}, "pick at least one niche."
    if not payload["content_formats"]:
        return {}, "pick at least one content format."

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
        "neighborhoods": CREATOR_NEIGHBORHOODS,
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
            "google_configured": False,
            "google_calendar_connected": False,
            "google_gmail_connected": False,
            "instagram_configured": False,
            "instagram_connected": False,
            "instagram_suggested_handle": None,
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
