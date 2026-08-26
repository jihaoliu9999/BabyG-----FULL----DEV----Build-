"""Creator dashboard, intel feedback, notifications, DMs, network,
calendar, jobs, content receipts, and performance insights.

v1 scope is creator-only. Brand-side read views (the creator's
read-only brand profile, brand→creator outreach notifications) shipped
in the full design but were removed when brand was deferred to v1.5
(see the brand-side-v1.5 branch).
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from app.core.rate_limit import dm_brief_manual_limiter
from app.core.redirects import safe_same_origin
from app.core.security import SessionPayload, clear_pending_role, clear_session
from app.core.templating import templates
from app.core.url_guard import http_url_or_none
from app.deps import require_role
from app.integrations import google_calendar, instagram_meta
from app.services import (
    audit,
    babyg_awareness,
    bookings,
    bot,
    bot_nudges,
    bot_prompts,
    calendar_sync,
    discover,
    discovery,
    dm_briefs,
    dms,
    greetings,
    intel,
    jobs,
    locations,
    network,
    notifications,
    oauth_connections,
    profiles,
    receipts,
    stats_merge,
    storage,
    views,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["creator"])

CATEGORY_LABELS = {
    "venue": "Venue",
    "trend": "Trend",
    "brand": "Brand",
    "collab": "Collab",
    "alert": "Alert",
}

PROFILE_CHIP_OPTIONS = {
    "niches": {
        "field": "niches",
        "options": [
            "lifestyle",
            "nightlife",
            "fashion",
            "fitness",
            "beauty",
            "food",
            "travel",
            "music",
        ],
    },
    "formats": {
        "field": "content_formats",
        "options": ["reels", "stories", "posts", "lives", "shorts"],
    },
    "limits": {
        "field": "hard_limits",
        "options": [
            "no alcohol",
            "no gambling",
            "no politics",
            "no adult content",
            "no smoking",
            "no explicit language",
        ],
    },
}
PROFILE_PLATFORM_OPTIONS = ["Instagram", "TikTok"]


def _calendar_preview_days(today: date | None = None) -> list[dict[str, Any]]:
    """Return the real five-day strip used by Creator Home."""
    start = today or date.today()
    return [
        {
            "weekday": (start + timedelta(days=offset)).strftime("%a"),
            "day": (start + timedelta(days=offset)).day,
            "is_today": offset == 0,
        }
        for offset in range(5)
    ]


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

    # "needs you" surfaces non-DM notifications only. DM alerts get their
    # own /creator/dm page; duplicating them here made the home feel
    # spammy and let a user tap into a thread from home instead of the
    # dedicated inbox. We keep the raw counts for the greeting summary
    # (so "3 things need you today" stays honest).
    unread_notifs_all = notifications.list_unread(session["user_id"], limit=8)
    unread_notifs = [n for n in unread_notifs_all if n.get("kind") != "new_dm"]
    non_dm_unread_total = len(unread_notifs)

    # Pending inbound connection requests power the accept/decline chips
    # on the top rows — clearest "waiting on you" action after DMs.
    try:
        pending_connections = network.list_incoming_pending(session["user_id"])
    except Exception:
        pending_connections = []

    # Home becomes the daily command center: include a compact calendar
    # preview so creators don't have to leave Home to see what's next.
    # Each helper degrades to an empty/false default so a flaky Supabase
    # or unconnected Google Calendar doesn't blank the dashboard.
    try:
        upcoming_bookings = bookings.list_for_user(
            session["user_id"], horizon="upcoming", limit=4
        )
    except Exception:
        upcoming_bookings = []
    try:
        google_connection = oauth_connections.get_google_connection(
            session["user_id"]
        )
        calendar_connected = oauth_connections.google_calendar_connected(
            google_connection
        )
    except Exception:
        calendar_connected = False

    # Matched picks — the babyg-curated discover feed, top 3 for home.
    # Whatever the discover service already filters/prioritizes for this
    # viewer flows through unchanged; this is just a smaller slice.
    viewer_location_label = ", ".join(
        p for p in (profile.get("location_city"), profile.get("location_region")) if p
    ) or None
    try:
        matched_picks = discover.list_cards(
            viewer_id=session["user_id"],
            viewer_role="creator",
            kind="all",
            viewer_tags=list(profile.get("niches") or []),
            viewer_location_label=viewer_location_label,
            viewer_platform=profile.get("primary_platform"),
            limit=3,
        )
    except Exception:
        matched_picks = []

    # "N things need you today" summary count: connections + confirm
    # bookings + non-DM notifications. DMs deliberately excluded so
    # the number matches what's shown in "needs you" below.
    needs_count = (
        len(pending_connections)
        + (1 if upcoming_bookings else 0)
        + non_dm_unread_total
    )

    first_name = (profile.get("full_name") or "creator").split(" ")[0].lower()
    daily_greeting = greetings.pick_daily(session["user_id"], first_name)
    profile_initial = (
        (profile.get("full_name") or profile.get("instagram_handle") or "c")[:1].upper()
    )

    return templates.TemplateResponse(
        request,
        "creator/dashboard.html",
        {
            "profile": profile,
            "profile_initial": profile_initial,
            "posts": posts,
            "feedback_map": feedback_map,
            "categories": list(CATEGORY_LABELS.items()),
            "active_category": category if category in intel.CATEGORIES else None,
            "unread_notifs": unread_notifs,
            "pending_connections": pending_connections,
            "upcoming_bookings": upcoming_bookings,
            "matched_picks": matched_picks,
            "needs_count": needs_count,
            "calendar_connected": calendar_connected,
            "calendar_days": _calendar_preview_days(),
            "daily_greeting": daily_greeting,
        },
    )


@router.get("/creator/bot", response_class=HTMLResponse)
async def bot_chat(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    # Proactive nudges — babyg drops a message the moment a fresh
    # discover match or an imminent pending booking lands. Deduped by
    # nudge_key so the same event never nudges twice. Wrapped so any
    # source failure never blanks the chat.
    try:
        bot_nudges.generate_pending(session["user_id"])
    except Exception:
        logger.exception("bot_nudges.generate_pending failed")

    messages = bot.list_messages(session["user_id"])

    # Compose the awareness snapshot once and reuse it for both the
    # composer chip strip AND the assistant system prompt on the next
    # turn (bot.list_messages doesn't need it, but the send path does).
    # Snapshot reads are ~30s cached per user so re-renders are cheap.
    try:
        snap = babyg_awareness.snapshot(session["user_id"])
    except Exception:
        logger.exception("babyg_awareness.snapshot failed")
        snap = {}

    # Composer chip strip. Rendered on every turn now (not just empty
    # threads) because chips reflect the live state of the world and
    # the user should always have something useful to tap.
    prompts = bot_prompts.compute_prompts(
        unread_dms_count=int((snap.get("unread_dms") or {}).get("count") or 0),
        recent_dm_peer_name=(snap.get("unread_dms") or {}).get("latest_peer_name"),
        snapshot=snap,
    )

    return templates.TemplateResponse(
        request,
        "creator/bot.html",
        {
            "profile": profile,
            "messages": messages,
            "error": None,
            "bot_prompts": prompts,
        },
    )


def _is_ajax(request: Request) -> bool:
    """True when bot.js submitted this POST via fetch().

    Branches on the X-Requested-With header bot.js sets. Native form
    posts (no JS, or JS failure) don't set the header, so they fall
    through to the existing PRG redirect — no behavior change for
    no-JS clients.
    """
    return request.headers.get("x-requested-with", "").lower() == "fetch"


def _bot_messages_partial(
    request: Request,
    user_id: str,
    *,
    error: str | None = None,
    status_code: int = 200,
) -> Response:
    """Render the bot_messages.html partial used by every async bot route."""
    messages = bot.list_messages(user_id)
    return templates.TemplateResponse(
        request,
        "_partials/bot_messages.html",
        {"messages": messages, "error": error},
        status_code=status_code,
    )


@router.post("/creator/bot")
async def bot_send(
    request: Request,
    message: str = Form(...),
    user_now_iso: str | None = Form(default=None),
    user_tz: str | None = Form(default=None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    result = bot.handle_creator_message(
        user_id=session["user_id"],
        content=message,
        user_now_iso=user_now_iso,
        user_tz=user_tz,
    )

    if _is_ajax(request):
        err = None if result.response else "babyg couldn't answer that turn. Try again."
        code = 200 if result.response else 400
        return _bot_messages_partial(
            request, session["user_id"], error=err, status_code=code
        )

    if not result.response:
        messages = bot.list_messages(session["user_id"])
        return templates.TemplateResponse(
            request,
            "creator/bot.html",
            {
                "profile": profile,
                "messages": messages,
                "error": "babyg couldn't answer that turn. Try again.",
            },
            status_code=400,
        )
    return RedirectResponse("/creator/bot", status_code=303)


@router.post("/creator/bot/actions/{message_id}/confirm")
async def bot_action_confirm(
    request: Request,
    message_id: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    result = bot.confirm_action(user_id=session["user_id"], message_id=message_id)
    if not result.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if _is_ajax(request):
        return _bot_messages_partial(request, session["user_id"])
    return RedirectResponse("/creator/bot", status_code=303)


@router.post("/creator/bot/actions/{message_id}/cancel")
async def bot_action_cancel(
    request: Request,
    message_id: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    result = bot.cancel_action(user_id=session["user_id"], message_id=message_id)
    if not result.found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if _is_ajax(request):
        return _bot_messages_partial(request, session["user_id"])
    return RedirectResponse("/creator/bot", status_code=303)


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


# -----------------------------------------------------------------------------
# Profile / settings
# -----------------------------------------------------------------------------


@router.get("/creator/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    raw_profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not raw_profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)
    profile = {
        **raw_profile,
        "location_label": profiles.safe_location_label(raw_profile),
    }
    # `profile_preview` is the public-projected view of the creator's own
    # row — what brands and peers see in Discover. Surfacing it at the
    # top of /creator/profile gives the creator an explicit mirror so
    # privacy + completeness decisions feel concrete instead of abstract.
    profile_preview = profiles.public_creator(raw_profile) or {}
    chip_values = _profile_chip_values(profile)
    return templates.TemplateResponse(
        request,
        "creator/profile.html",
        {
            "profile": profile,
            "profile_preview": profile_preview,
            "profile_chip_values": chip_values,
            "profile_chip_options": _profile_chip_options(chip_values),
        },
    )


@router.post("/creator/profile/chips")
async def profile_chips_update(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    form = await request.form()
    section = str(form.get("section") or "").strip()
    config = PROFILE_CHIP_OPTIONS.get(section)
    if config is None:
        return RedirectResponse("/creator/profile?chips=invalid", status_code=303)

    field = str(config["field"])
    current = _profile_chip_values(profile).get(section, [])
    allowed = _dedupe([*config["options"], *current])
    values = _multi_clean(form.getlist("values"), allowed)

    if not profiles.update_creator_profile(session["user_id"], {field: values}):
        return RedirectResponse("/creator/profile?chips=save_failed", status_code=303)
    return RedirectResponse("/creator/profile?chips=ok", status_code=303)


@router.post("/creator/profile/bio")
async def profile_bio_update(
    bio: str = Form(""),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    cleaned = "\n".join(line.strip() for line in bio.strip().splitlines()).strip()
    if not profiles.update_creator_profile(
        session["user_id"], {"bio": cleaned[:600] or None}
    ):
        return RedirectResponse("/creator/profile?bio=save_failed", status_code=303)
    return RedirectResponse("/creator/profile?bio=ok", status_code=303)


@router.post("/creator/profile/location")
async def profile_location_update(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    form = await request.form()
    payload, error = locations.profile_location_payload(form)
    if error:
        return RedirectResponse("/creator/profile?details=invalid_location", status_code=303)
    if "primary_platform" in form:
        platform = str(form.get("primary_platform") or "").strip()
        if platform:
            normalized_platform = next(
                (
                    option
                    for option in PROFILE_PLATFORM_OPTIONS
                    if option.lower() == platform.lower()
                ),
                None,
            )
            if normalized_platform is None:
                return RedirectResponse(
                    "/creator/profile?details=invalid_details", status_code=303
                )
            payload["primary_platform"] = normalized_platform
        else:
            payload["primary_platform"] = None
    if not profiles.update_creator_profile(session["user_id"], payload):
        return RedirectResponse("/creator/profile?details=save_failed", status_code=303)
    return RedirectResponse("/creator/profile?details=ok", status_code=303)


@router.post("/creator/profile/deals")
async def profile_deals_update(
    deal_min_rate_text: str = Form(""),
    deal_usage_rights_default: str = Form(""),
    deal_travel_willingness: str = Form(""),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Update the deal preferences section: rate floor (free text), the
    default usage-rights posture, and travel willingness. All three are
    owner-private — they inform babyg drafts and discover-quality
    ranking but never appear in `public_creator()`."""
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    payload: dict[str, Any] = {}
    rate = " ".join(deal_min_rate_text.strip().split())[:120]
    # Clear-on-empty so the creator can blank it out.
    payload["deal_min_rate_text"] = rate or None
    usage = deal_usage_rights_default.strip().lower()
    if usage in profiles.DEAL_USAGE_RIGHTS_VALUES:
        payload["deal_usage_rights_default"] = usage
    elif usage == "":
        payload["deal_usage_rights_default"] = None
    travel = deal_travel_willingness.strip().lower()
    if travel in profiles.DEAL_TRAVEL_WILLINGNESS_VALUES:
        payload["deal_travel_willingness"] = travel
    elif travel == "":
        payload["deal_travel_willingness"] = None
    if not profiles.update_creator_profile(session["user_id"], payload):
        return RedirectResponse("/creator/profile?deals=save_failed", status_code=303)
    return RedirectResponse("/creator/profile?deals=ok", status_code=303)


@router.post("/creator/profile/privacy")
async def profile_privacy_update(
    dm_preference: str = Form(""),
    location_display_level: str = Form(""),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Update the privacy section — who can DM and how much of the
    creator's location is exposed via `public_creator()`. Both values
    are closed-vocabulary (see DM_PREFERENCE_VALUES /
    LOCATION_DISPLAY_LEVELS in app/services/profiles.py)."""
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    payload: dict[str, str] = {}
    dm = dm_preference.strip().lower()
    if dm in profiles.DM_PREFERENCE_VALUES:
        payload["dm_preference"] = dm
    loc = location_display_level.strip().lower()
    if loc in profiles.LOCATION_DISPLAY_LEVELS:
        payload["location_display_level"] = loc
    if not payload:
        return RedirectResponse(
            "/creator/profile/settings?privacy=invalid", status_code=303
        )
    if not profiles.update_creator_profile(session["user_id"], payload):
        return RedirectResponse(
            "/creator/profile/settings?privacy=save_failed", status_code=303
        )
    return RedirectResponse("/creator/profile/settings?privacy=ok", status_code=303)


@router.post("/creator/profile/babyg")
async def profile_babyg_update(
    babyg_tone: str = Form(""),
    babyg_risk_tolerance: str = Form(""),
    babyg_auto_brief_dms: str = Form(""),
    babyg_email_assistance: str = Form(""),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Update the babyg-behavior section — tone, risk tolerance, and
    the two opt-in toggles (auto-brief DMs, email assistance). The
    booleans accept any HTML-form-truthy value ("on", "true", "1") so
    a missing checkbox cleanly maps to false."""
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    payload: dict[str, Any] = {}
    tone = babyg_tone.strip().lower()
    if tone in profiles.BABYG_TONES:
        payload["babyg_tone"] = tone
    risk = babyg_risk_tolerance.strip().lower()
    if risk in profiles.BABYG_RISK_TOLERANCES:
        payload["babyg_risk_tolerance"] = risk
    payload["babyg_auto_brief_dms"] = _form_bool(babyg_auto_brief_dms)
    payload["babyg_email_assistance"] = _form_bool(babyg_email_assistance)
    if not profiles.update_creator_profile(session["user_id"], payload):
        return RedirectResponse(
            "/creator/profile/settings?babyg=save_failed", status_code=303
        )
    return RedirectResponse("/creator/profile/settings?babyg=ok", status_code=303)


def _form_bool(value: str) -> bool:
    """HTML checkboxes submit ``"on"`` (or nothing); the magic-link
    style settings forms below sometimes ship the value as ``"true"``.
    Treat anything truthy as true so the route stays template-agnostic.
    """
    return value.strip().lower() in {"on", "true", "1", "yes"}


@router.get("/creator/profile/settings", response_class=HTMLResponse)
async def profile_settings_page(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    raw_profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not raw_profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)
    profile = {**raw_profile, "location_label": profiles.safe_location_label(raw_profile)}
    profile_preview = profiles.public_creator(raw_profile) or {}
    chip_values = _profile_chip_values(profile)
    google_connection = oauth_connections.get_google_connection(session["user_id"])
    # Defensive: a test environment without Supabase configured must
    # still render the page. The worst case is the IG card showing
    # "coming soon" instead of "ready" / "connected".
    try:
        instagram_configured = instagram_meta.is_configured()
    except Exception:
        instagram_configured = False
    try:
        instagram_connected = (
            oauth_connections.get_instagram_connection(session["user_id"]) is not None
        )
    except Exception:
        instagram_connected = False
    return templates.TemplateResponse(
        request,
        "creator/profile_settings.html",
        {
            "profile": profile,
            "profile_preview": profile_preview,
            "profile_chip_values": chip_values,
            "profile_chip_options": _profile_chip_options(chip_values),
            "google_calendar_connected": oauth_connections.google_calendar_connected(
                google_connection
            ),
            "google_gmail_connected": oauth_connections.google_gmail_connected(
                google_connection
            ),
            "google_configured": google_calendar.is_configured(),
            "instagram_configured": instagram_configured,
            "instagram_connected": instagram_connected,
        },
    )


@router.get("/creator/_debug/integrations")
async def integrations_debug(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Operational diagnostic: shows what env-driven integration state
    the running process actually sees.

    Returns booleans only — never tokens, app IDs, or secrets. The
    resolved redirect URIs are surfaced because they must match the
    OAuth provider's allow-list verbatim, and copy-pasting them out of
    Railway env is the #1 cause of OAuth-side breakage.

    Creator-protected (require_role) so this endpoint is not public.
    """
    try:
        google_configured = google_calendar.is_configured()
    except Exception:
        google_configured = False
    try:
        google_redirect = google_calendar.redirect_uri()
    except Exception:
        google_redirect = None
    try:
        instagram_configured = instagram_meta.is_configured()
    except Exception:
        instagram_configured = False
    try:
        instagram_redirect = instagram_meta.redirect_uri()
    except Exception:
        instagram_redirect = None
    from app.config import get_settings

    settings = get_settings()
    return JSONResponse(
        {
            "user_id": session["user_id"],
            "app_url": settings.app_url,
            "google": {
                "configured": google_configured,
                "redirect_uri_sent_to_provider": google_redirect,
            },
            "instagram": {
                "configured": instagram_configured,
                "redirect_uri_sent_to_provider": instagram_redirect,
            },
        }
    )


@router.post("/creator/profile/photo")
async def profile_photo_upload(
    request: Request,
    photo: UploadFile = File(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    # Intentionally NOT gated on onboarding_completed_at — step 3 of the
    # onboarding wizard uses this same endpoint, so a creator can upload
    # before their profile flips to completed. Auth (require_role) +
    # storage validation are still in place.
    raw = await photo.read()
    if not raw:
        return RedirectResponse(
            "/creator/profile?photo=missing", status_code=303
        )
    try:
        url = storage.upload_profile_photo(
            session["user_id"], raw, photo.content_type
        )
    except storage.PhotoTooLargeError:
        return RedirectResponse(
            "/creator/profile?photo=too_big", status_code=303
        )
    except storage.PhotoUnsupportedTypeError:
        return RedirectResponse(
            "/creator/profile?photo=bad_type", status_code=303
        )
    except storage.PhotoDecodeError:
        return RedirectResponse(
            "/creator/profile?photo=corrupt", status_code=303
        )
    except storage.PhotoStorageError:
        # Supabase itself rejected the upload (network, bucket cap, RLS,
        # creds). The exception is already logged status-only inside
        # storage.py — show a clean flash, don't crash the request.
        return RedirectResponse(
            "/creator/profile?photo=storage_failed", status_code=303
        )

    current_profile = profiles.get_creator_profile(session["user_id"]) or {}
    old_url = current_profile.get("profile_photo_url")
    if not profiles.update_creator_profile(
        session["user_id"], {"profile_photo_url": url}
    ):
        # DB update failed but the file is in storage; the next successful
        # upload will overwrite. Surface a generic error.
        return RedirectResponse(
            "/creator/profile?photo=save_failed", status_code=303
        )
    if old_url and old_url != url:
        storage.delete_profile_photo(session["user_id"], old_url)
    return RedirectResponse("/creator/profile?photo=ok", status_code=303)


@router.post("/creator/profile/photo/delete")
async def profile_photo_delete(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    # Clear the DB column first (source of truth for "no photo"), then
    # best-effort remove the storage object.
    current_profile = profiles.get_creator_profile(session["user_id"]) or {}
    profiles.update_creator_profile(
        session["user_id"], {"profile_photo_url": None}
    )
    storage.delete_profile_photo(
        session["user_id"], current_profile.get("profile_photo_url")
    )
    return RedirectResponse("/creator/profile?photo=removed", status_code=303)


def _profile_chip_values(profile: dict) -> dict[str, list[str]]:
    return {
        section: _clean_profile_values(profile.get(str(config["field"])))
        for section, config in PROFILE_CHIP_OPTIONS.items()
    }


def _profile_chip_options(chip_values: dict[str, list[str]]) -> dict[str, list[str]]:
    return {
        section: _dedupe([*config["options"], *chip_values.get(section, [])])
        for section, config in PROFILE_CHIP_OPTIONS.items()
    }


def _clean_profile_values(value) -> list[str]:
    if value is None:
        return []
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, list | tuple):
        return []
    return _dedupe(str(item).strip() for item in raw if str(item).strip())


def _multi_clean(values, allowed: list[str]) -> list[str]:
    allowed_set = set(allowed)
    return _dedupe(
        str(value).strip()
        for value in values
        if str(value).strip() in allowed_set
    )


def _dedupe(values) -> list[str]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return out


# -----------------------------------------------------------------------------
# Notifications
# -----------------------------------------------------------------------------


@router.get("/creator/notifications", response_class=HTMLResponse)
async def notifications_list(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    rows = notifications.list_for_user(session["user_id"], limit=100)
    return templates.TemplateResponse(
        request,
        "creator/notifications.html",
        {"rows": rows},
    )


@router.post("/creator/notifications/{notification_id}/read")
async def notifications_mark_read(
    notification_id: str,
    session: SessionPayload = Depends(require_role("creator")),
    target: str = Form("/creator/notifications"),
) -> Response:
    notifications.mark_read(
        user_id=session["user_id"], notification_id=notification_id
    )
    return RedirectResponse(
        safe_same_origin(target, default="/creator/notifications"),
        status_code=303,
    )


@router.post("/creator/notifications/read-all")
async def notifications_mark_all_read(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    notifications.mark_all_read(session["user_id"])
    return RedirectResponse("/creator/notifications", status_code=303)


# -----------------------------------------------------------------------------
# DMs
# -----------------------------------------------------------------------------


@router.get("/creator/dm", response_class=HTMLResponse)
async def dm_list(
    request: Request,
    q: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    threads = dms.list_threads_for_user(session["user_id"])
    # v1 is creator-only — peer is always another connected creator.
    peer_ids = sorted({str(t["peer_id"]) for t in threads})
    peers = profiles.get_creators_by_ids(peer_ids)
    peer_kinds = {pid: ("creator" if pid in peers else "unknown") for pid in peer_ids}
    dm_query = " ".join((q or "").split())[:80]
    if dm_query:
        terms = [term.casefold() for term in dm_query.split()]

        def _matches_thread(thread: dict[str, Any]) -> bool:
            peer_id = str(thread.get("peer_id") or "")
            peer = peers.get(peer_id) or {}
            haystack = " ".join(
                str(value or "")
                for value in (
                    peer.get("full_name"),
                    peer.get("instagram_handle"),
                    peer_id,
                )
            ).casefold()
            return all(term in haystack for term in terms)

        threads = [thread for thread in threads if _matches_thread(thread)]
    thread_ids = [str(t["id"]) for t in threads]
    # Private babyg risk chips: latest brief per thread, recipient-scoped.
    briefs = dm_briefs.latest_briefs_for_threads(
        thread_ids, recipient_id=session["user_id"]
    )
    # Per-thread unread count powers the coral bar + bold-name state
    # and the "N unread" line in the header. Single batched query.
    unread_by_thread = dms.unread_counts_by_thread(session["user_id"], thread_ids)
    unread_total = sum(unread_by_thread.values())
    # Preview text under every row — the actual last message, batched.
    # Falls back to babyg brief in the template when a thread has a brief.
    last_messages = dms.last_messages_by_thread(thread_ids)
    return templates.TemplateResponse(
        request,
        "creator/dm_list.html",
        {
            "threads": threads,
            "peers": peers,
            "peer_kinds": peer_kinds,
            "briefs": briefs,
            "unread_by_thread": unread_by_thread,
            "unread_total": unread_total,
            "last_messages": last_messages,
            "thread_count": len(threads),
            "dm_query": dm_query,
        },
    )


@router.get("/creator/dm/{peer_user_id}", response_class=HTMLResponse)
async def dm_thread(
    peer_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    peer, peer_kind = _resolve_creator_dm_peer(
        me_id=session["user_id"], peer_user_id=peer_user_id
    )
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    messages = dms.list_messages(
        str(thread["id"]), participant_id=session["user_id"]
    )
    dms.mark_thread_read_for(str(thread["id"]), reader_id=session["user_id"])

    # Private babyg brief for the latest INCOMING message (best-effort:
    # auto-generates only for "serious" messages, never blocks the render,
    # and stays invisible to the other party). Manual refresh lives at
    # POST /creator/dm/{peer}/brief ("ask babyg").
    brief, brief_message_id = _latest_incoming_brief(
        thread=thread, messages=messages, me_id=session["user_id"], peer=peer
    )
    return templates.TemplateResponse(
        request,
        "creator/dm_thread.html",
        {
            "thread": thread,
            "messages": messages,
            "peer": peer,
            "peer_id": peer_user_id,
            "peer_kind": peer_kind,
            "me_id": session["user_id"],
            "brief": brief,
            "brief_message_id": brief_message_id,
        },
    )


def _latest_incoming_brief(
    *, thread: dict, messages: list[dict], me_id: str, peer: dict, force: bool = False
) -> tuple[dict | None, str | None]:
    """Return (brief, message_id) for the most recent message the peer
    sent (incoming to me). Best-effort; auto-generation is gated to
    serious messages inside the service. Returns (None, msg_id) when
    there's an incoming message but no brief yet."""
    incoming = [m for m in messages if str(m.get("sender_id")) != me_id]
    if not incoming:
        return None, None
    latest = incoming[-1]
    message_id = str(latest.get("id") or "")
    sender_public = profiles.public_creator(peer) or {}
    me_full = profiles.get_creator_profile(me_id)
    recipient_public = profiles.public_creator(me_full) or {}
    auto_enabled = not (
        me_full is not None and me_full.get("babyg_auto_brief_dms") is False
    )
    try:
        brief = dm_briefs.get_or_generate_brief(
            thread_id=str(thread["id"]),
            message=latest,
            recipient_id=me_id,
            sender_public=sender_public,
            recipient_public=recipient_public,
            recent_messages=messages,
            auto_enabled=auto_enabled,
            force=force,
        )
    except Exception:  # brief must never break the thread render
        brief = dm_briefs.get_brief_for_message(message_id, recipient_id=me_id)
    return brief, (message_id or None)


@router.post("/creator/dm/{peer_user_id}/send")
async def dm_send(
    peer_user_id: str,
    body: str = Form(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    peer, _peer_kind = _resolve_creator_dm_peer(
        me_id=session["user_id"], peer_user_id=peer_user_id
    )
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    body = (body or "").strip()
    if not body:
        return RedirectResponse(f"/creator/dm/{peer_user_id}", status_code=303)

    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    msg = dms.send_message(
        thread_id=str(thread["id"]),
        sender_id=session["user_id"],
        body=body,
    )
    if msg is not None:
        creator_profile = profiles.get_creator_profile(session["user_id"]) or {}
        sender_label = (
            creator_profile.get("full_name")
            or creator_profile.get("instagram_handle")
            or "a creator"
        )
        # v1 is creator-only: the recipient always reads at
        # /creator/dm/{me}. peer_kind is unused but kept on the return
        # tuple so future v1.5 work can re-introduce role-based routing.
        target = f"/creator/dm/{session['user_id']}"
        notifications.create(
            user_id=peer_user_id,
            kind="new_dm",
            title=f"New message from {sender_label}",
            body=body[:160],
            link_path=target,
        )
    return RedirectResponse(f"/creator/dm/{peer_user_id}", status_code=303)


@router.post("/creator/dm/{peer_user_id}/brief")
async def dm_brief(
    peer_user_id: str,
    request: Request,
    next: str | None = Form(default=None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Manual "ask babyg" — (re)generate the private brief for the latest
    incoming message in this thread. Recipient-only, read-only, never
    sends anything. Returns JSON for the AJAX path, redirect for no-JS.

    ``next`` (optional) — same-origin path to redirect to after the
    brief lands. Used by the inbox "ask babyg to read" ghost chip so
    the user stays on the inbox instead of getting bounced into the
    thread they never opened.
    """
    peer, _kind = _resolve_creator_dm_peer(
        me_id=session["user_id"], peer_user_id=peer_user_id
    )
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    safe_next = safe_same_origin(next) if next else None
    if not dm_brief_manual_limiter.allow(
        "dm-brief-manual", session["user_id"]
    ):
        if request.headers.get("X-Requested-With") == "fetch":
            return JSONResponse(
                {"ok": False, "error": "rate_limited"}, status_code=429
            )
        return RedirectResponse(
            f"/creator/dm/{peer_user_id}?brief_rate_limited=1", status_code=303
        )
    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    messages = dms.list_messages(
        str(thread["id"]), participant_id=session["user_id"]
    )
    brief, _mid = _latest_incoming_brief(
        thread=thread, messages=messages, me_id=session["user_id"],
        peer=peer, force=True,
    )
    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse(
            {"ok": brief is not None, "brief": _brief_public(brief)}
        )
    return RedirectResponse(
        safe_next or f"/creator/dm/{peer_user_id}", status_code=303
    )


@router.post("/creator/dm/{peer_user_id}/brief/follow-up")
async def dm_brief_follow_up(
    peer_user_id: str,
    request: Request,
    focus: str = Form(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Return ephemeral private analysis. It never updates the canonical brief."""
    if focus not in dm_briefs.FOLLOW_UP_FOCUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    peer, _kind = _resolve_creator_dm_peer(
        me_id=session["user_id"], peer_user_id=peer_user_id
    )
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not dm_brief_manual_limiter.allow(
        "dm-brief-manual", session["user_id"]
    ):
        return JSONResponse(
            {"ok": False, "error": "rate_limited"}, status_code=429
        )
    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    messages = dms.list_messages(
        str(thread["id"]), participant_id=session["user_id"]
    )
    result = dm_briefs.generate_follow_up(
        focus=focus,
        messages=messages,
        recipient_id=session["user_id"],
        sender_public=profiles.public_creator(peer) or {},
    )
    return JSONResponse({"ok": result is not None, "result": result})


def _brief_public(brief: dict | None) -> dict | None:
    """Shape a brief for the recipient's own AJAX consumption (display
    fields only)."""
    if not brief:
        return None
    keys = (
        "risk_level", "risk_reasons", "summary", "missing_terms",
        "recommended_next_action", "suggested_reply", "trust_notes",
        "intent_type", "confidence_level", "sender_ask", "why_it_matters",
        "deal_terms", "deal_stage", "message_annotations", "reply_options",
        "generated_at",
    )
    return {k: brief.get(k) for k in keys}


def _resolve_creator_dm_peer(
    *, me_id: str, peer_user_id: str
) -> tuple[dict | None, str]:
    """Look up the DM peer for a creator-side route.

    v1 is creator-only: the peer must be another onboarded creator the
    caller has an `accepted` connection with (this gate deters cold
    messaging). Brand-side DM peers shipped in the full v1 design but
    are deferred to v1.5 (see brand-side-v1.5 branch).

    Returns (peer_profile, "creator") on success, (None, "") otherwise.
    """
    if peer_user_id == me_id:
        return None, ""
    creator = profiles.get_creator_profile(peer_user_id)
    if creator is None or not creator.get("onboarding_completed_at"):
        return None, ""

    conn = network.get_connection_between(me_id, peer_user_id)
    if conn is None or conn.get("status") != "accepted":
        return None, ""
    return creator, "creator"


# -----------------------------------------------------------------------------
# Network: directory + peer profile + connections
# -----------------------------------------------------------------------------


@router.get("/creator/network", response_class=HTMLResponse)
async def network_swipe_page(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
    bring_back: str | None = Query(default=None),
    mode: str = Query(default="both"),
    skip_posting: str | None = Query(default=None),
) -> Response:
    """Swipe-style discovery feed.

    Renders the next batch of candidate creators from the discovery
    service. The first card is shown front-and-center; the next few
    are prefetched into hidden DOM so swipes feel instant. When the
    stack is empty we render a clean empty state, not a blank page.

    A `viewed` action is recorded for whichever creator lands on top
    of the stack so we don't show them again in the same session if
    the viewer refreshes mid-card.

    `bring_back` (set by the undo flow) floats a just-restored creator
    back to the front of the stack so the undo lands them on the active
    card.
    """
    user_id = session["user_id"]
    discovery_mode = _clean_network_mode(mode)
    people_stack = (
        discovery.next_stack_for(user_id, prioritize_user_id=bring_back or None)
        if discovery_mode in {"people", "both"}
        else []
    )
    people_cards = [_network_creator_card(row) for row in people_stack]
    posting_cards = (
        _network_posting_cards(
            user_id=user_id,
            skip_ids={skip_posting} if skip_posting else set(),
        )
        if discovery_mode in {"postings", "both"}
        else []
    )
    stack = _merge_network_cards(people_cards, posting_cards, discovery_mode)
    pending_in = len(network.list_incoming_pending(user_id))
    can_undo = discovery.last_undoable_pass(user_id) is not None
    if stack and stack[0].get("card_type") == "person":
        # Record a view for the top card. Best-effort — never blocks
        # the render.
        discovery.record_action(
            user_id=user_id,
            target_user_id=str(stack[0].get("user_id") or ""),
            action_type="viewed",
        )
    return templates.TemplateResponse(
        request,
        "creator/network_swipe.html",
        {
            "stack": stack,
            "pending_in": pending_in,
            "can_undo": can_undo,
            "discovery_mode": discovery_mode,
        },
    )


@router.post("/creator/network/undo")
async def network_undo_pass(
    mode: str = Form("both"),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Undo the viewer's most recent pass.

    Records an `undo_pass` discovery action against the most recently
    passed creator (so the stack no longer treats them as passed) and
    redirects back to the swipe page with that creator floated to the
    top. If there is nothing to undo, this is a harmless no-op redirect.
    """
    user_id = session["user_id"]
    target_id = discovery.last_undoable_pass(user_id)
    if not target_id:
        return RedirectResponse("/creator/network", status_code=303)
    discovery_mode = _clean_network_mode(mode)
    discovery.record_action(
        user_id=user_id,
        target_user_id=target_id,
        action_type="undo_pass",
    )
    return RedirectResponse(
        f"/creator/network?mode={discovery_mode}&bring_back={target_id}",
        status_code=303,
    )


@router.post("/creator/network/swipe")
async def network_swipe_action(
    target_user_id: str = Form(...),
    action: str = Form(...),
    mode: str = Form("both"),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Record a swipe action and redirect back to the swipe page.

    Action whitelist mirrors discovery.ALLOWED_ACTIONS minus `viewed`
    (that's recorded server-side, not user-driven). `connected` also
    fires a real connection request — reusing the existing flow so
    duplicates are deduped at the storage layer.

    The redirect is a 303 to /creator/network so the next GET re-runs
    `discovery.next_stack_for` and excludes the just-acted-on target.
    """
    user_id = session["user_id"]
    action_clean = (action or "").strip().lower()
    discovery_mode = _clean_network_mode(mode)
    if action_clean not in {"passed", "connected", "skipped", "opened_profile"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if target_user_id == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    peer = profiles.get_creator_profile(target_user_id)
    if peer is None or not peer.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    discovery.record_action(
        user_id=user_id,
        target_user_id=target_user_id,
        action_type=action_clean,
    )

    if action_clean == "connected" and network.request_connection(
        requester_id=user_id, addressee_id=target_user_id
    ):
        # Reuse the canonical flow; duplicates are no-ops there.
        notifications.create(
            user_id=target_user_id,
            kind="connection_request",
            title="Someone wants to connect.",
            body=None,
            link_path="/creator/connections",
        )

    if action_clean == "opened_profile":
        return RedirectResponse(
            f"/creator/network/{target_user_id}", status_code=303
        )
    return RedirectResponse(f"/creator/network?mode={discovery_mode}", status_code=303)


@router.post("/creator/network/posting")
async def network_posting_action(
    listing_id: str = Form(...),
    action: str = Form(...),
    mode: str = Form("both"),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Creator discovery action for postings.

    Posting storage does not currently have a durable per-viewer pass
    ledger. This route keeps the no-JS action path working and excludes
    a passed posting from the immediate refreshed stack without changing
    schema or repurposing the user-to-user discovery table.
    """
    listing = jobs.get(listing_id)
    if listing is None or listing.get("is_taken_down"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    is_mine = str(listing.get("poster_user_id") or "") == session["user_id"]
    if not is_mine and not listing.get("is_active"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    action_clean = (action or "").strip().lower()
    if action_clean not in {"passed", "opened_posting"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if action_clean == "opened_posting":
        return RedirectResponse(f"/creator/jobs/{listing_id}", status_code=303)

    discovery_mode = _clean_network_mode(mode)
    return RedirectResponse(
        f"/creator/network?mode={discovery_mode}&skip_posting={listing_id}",
        status_code=303,
    )


@router.get("/creator/network/{peer_user_id}", response_class=HTMLResponse)
async def network_profile(
    peer_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if peer_user_id == session["user_id"]:
        return RedirectResponse("/creator/network", status_code=302)

    peer_full = profiles.get_creator_profile(peer_user_id)
    if peer_full is None or not peer_full.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    peer = profiles.public_creator(peer_full)
    if peer is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    connection = network.get_connection_between(session["user_id"], peer_user_id)
    if connection is not None and connection.get("status") == "blocked":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    # Record the view (best-effort; don't block render). The view
    # ledger is the legacy/internal one; opened_profile is the
    # discovery-stack signal that excludes this peer from future
    # swipe cards.
    views.record_view(viewer_id=session["user_id"], viewed_id=peer_user_id)
    discovery.record_action(
        user_id=session["user_id"],
        target_user_id=peer_user_id,
        action_type="opened_profile",
    )

    return templates.TemplateResponse(
        request,
        "creator/network_profile.html",
        {
            "peer": peer,
            "peer_id": peer_user_id,
            "connection": connection,
            "state": _connection_state(connection, me_id=session["user_id"]),
        },
    )


@router.post("/creator/connections/request")
async def connection_request(
    peer_user_id: str = Form(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if peer_user_id == session["user_id"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    peer = profiles.get_creator_profile(peer_user_id)
    if peer is None or not peer.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if network.request_connection(
        requester_id=session["user_id"], addressee_id=peer_user_id
    ):
        notifications.create(
            user_id=peer_user_id,
            kind="connection_request",
            title="Someone wants to connect.",
            body=None,
            link_path="/creator/connections",
        )
    return RedirectResponse(f"/creator/network/{peer_user_id}", status_code=303)


@router.get("/creator/connections", response_class=HTMLResponse)
async def connections_list(
    request: Request,
    filter: str = "all",
    q: str = "",
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    accepted = network.list_accepted_for_user(session["user_id"])
    incoming = network.list_incoming_pending(session["user_id"])
    outgoing = network.list_outgoing_pending(session["user_id"])

    peer_ids = sorted({str(row["peer_id"]) for row in accepted + incoming + outgoing})
    peers = profiles.get_creators_by_ids(peer_ids)

    active_filter = filter if filter in {"all", "creators", "brands", "sent"} else "all"

    # Every peer on the creator side today is a creator (creator_connections
    # table). "brands" is wired for when brand↔creator connections land.
    creator_count = len(accepted)
    brand_count = 0

    return templates.TemplateResponse(
        request,
        "creator/connections_list.html",
        {
            "accepted": accepted,
            "incoming": incoming,
            "outgoing": outgoing,
            "peers": peers,
            "active_filter": active_filter,
            "search_query": (q or "").strip()[:80],
            "creator_count": creator_count,
            "brand_count": brand_count,
        },
    )


@router.post("/creator/connections/{connection_id}/disconnect")
async def connection_disconnect(
    connection_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """Tear down an accepted connection (either party can disconnect).

    Declared before the generic `{action}` route so the literal path
    wins. Flips the row to `removed` — no profile or message history is
    deleted. Returns JSON for the AJAX path (immediate UI update) and a
    redirect for the no-JS fallback.
    """
    ok = network.disconnect_connection(
        connection_id=connection_id, user_id=session["user_id"]
    )
    if ok:
        audit.record(
            actor_user_id=session["user_id"],
            action="connection.disconnect",
            target_type="connection",
            target_id=connection_id,
        )
    if request.headers.get("X-Requested-With") == "fetch":
        return JSONResponse(
            {"ok": ok},
            status_code=status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST,
        )
    return RedirectResponse("/creator/connections", status_code=303)


@router.post("/creator/connections/{connection_id}/{action}")
async def connection_respond(
    connection_id: str,
    action: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if action not in network.RESPOND_ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    if network.respond_to_connection(
        connection_id=connection_id,
        responder_id=session["user_id"],
        action=action,
    ):
        # Mirror brand-verify / abuse-resolve trail. Network actions are
        # user-initiated rather than operator-initiated, but the audit_log
        # is the right home for them too — operators reviewing a network
        # dispute will want the timeline.
        audit.record(
            actor_user_id=session["user_id"],
            action=f"connection.{action}",
            target_type="connection",
            target_id=connection_id,
        )
    return RedirectResponse("/creator/connections", status_code=303)


def _connection_state(connection, *, me_id: str) -> str:
    """Returns one of: none | outgoing_pending | incoming_pending |
    connected | declined | blocked. The template uses this to render
    the right CTA on the network profile page."""
    if connection is None:
        return "none"
    s = connection.get("status")
    if s == "accepted":
        return "connected"
    if s == "declined":
        return "declined"
    if s == "blocked":
        return "blocked"
    if s == "pending":
        return (
            "outgoing_pending"
            if connection.get("requester_id") == me_id
            else "incoming_pending"
        )
    return "none"


_NETWORK_MODES = {"people", "postings", "both"}


def _clean_network_mode(mode: str | None) -> str:
    mode_clean = (mode or "both").strip().lower()
    return mode_clean if mode_clean in _NETWORK_MODES else "both"


def _merge_network_cards(
    people_cards: list[dict],
    posting_cards: list[dict],
    mode: str,
) -> list[dict]:
    if mode == "people":
        return people_cards
    if mode == "postings":
        return posting_cards

    merged: list[dict] = []
    max_len = max(len(people_cards), len(posting_cards))
    for idx in range(max_len):
        if idx < len(people_cards):
            merged.append(people_cards[idx])
        if idx < len(posting_cards):
            merged.append(posting_cards[idx])
    return merged


def _network_creator_card(row: dict) -> dict:
    card = dict(row)
    card["card_type"] = "person"
    card["display_name"] = _public_network_name(card)
    card["public_role_label"] = _public_network_role(card)
    return card


def _network_posting_cards(
    *,
    user_id: str,
    skip_ids: set[str],
    limit: int = 8,
) -> list[dict]:
    listings = jobs.list_active(limit=limit)
    poster_ids = sorted({str(row.get("poster_user_id") or "") for row in listings})
    poster_profiles = profiles.get_creators_by_ids([pid for pid in poster_ids if pid])
    cards: list[dict] = []
    for row in listings:
        listing_id = str(row.get("id") or "")
        if not listing_id or listing_id in skip_ids:
            continue
        poster_id = str(row.get("poster_user_id") or "")
        poster = poster_profiles.get(poster_id) or {}
        cards.append(
            {
                "card_type": "posting",
                "id": listing_id,
                "poster_user_id": poster_id,
                "title": (row.get("title") or "posting").strip(),
                "description": (row.get("description") or "").strip(),
                "listing_type": str(row.get("listing_type") or "posting").replace(
                    "_", " "
                ),
                "compensation_text": row.get("compensation_text"),
                "deadline": row.get("deadline"),
                "target_niches": row.get("target_niches") or [],
                "poster_name": _public_network_name(poster),
                "poster_role_label": _public_network_role(poster),
                "is_mine": poster_id == user_id,
            }
        )
    return cards


def _public_network_name(profile: dict | None) -> str:
    if not profile:
        return "creator"
    raw = (
        str(profile.get("full_name") or "").strip()
        or str(profile.get("instagram_handle") or "").strip()
        or "creator"
    )
    cleaned = re.sub(r"^\s*(operator|admin)\s+", "", raw, flags=re.I).strip()
    return cleaned or "creator"


def _public_network_role(profile: dict | None) -> str:
    if not profile:
        return "creator"
    raw = str(
        profile.get("public_role")
        or profile.get("profile_type")
        or profile.get("account_type")
        or profile.get("role")
        or ""
    ).lower()
    return "brand" if "brand" in raw else "creator"


# -----------------------------------------------------------------------------
# Profile views (incoming, tier-gated)
# -----------------------------------------------------------------------------


@router.get("/creator/views", response_class=HTMLResponse)
async def views_list(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/creator", status_code=302)

    tier = profile.get("tier") or "basic"
    count = views.count_distinct_viewers(session["user_id"]) if tier in ("pro", "vip") else 0
    viewers: list[dict] = []
    viewer_profiles: dict[str, dict | None] = {}
    if tier == "vip":
        rows = views.list_recent_viewers(session["user_id"])
        viewers = rows
        viewer_profiles = {
            str(r["viewer_id"]): profiles.get_creator_profile(str(r["viewer_id"]))
            for r in rows
        }
    return templates.TemplateResponse(
        request,
        "creator/views.html",
        {
            "tier": tier,
            "count": count,
            "viewers": viewers,
            "viewer_profiles": viewer_profiles,
        },
    )


# -----------------------------------------------------------------------------
# Postings (creator-side; internal route/service names remain jobs/listings)
# -----------------------------------------------------------------------------


JOB_TYPES = list(jobs.LISTING_TYPES)


@router.get("/creator/jobs", response_class=HTMLResponse)
async def jobs_board(
    request: Request,
    niche: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listings = jobs.list_active(niche=niche)
    poster_ids = sorted({str(lst["poster_user_id"]) for lst in listings})
    poster_profiles = profiles.get_creators_by_ids(poster_ids)
    return templates.TemplateResponse(
        request,
        "creator/jobs_list.html",
        {
            "listings": listings,
            "poster_profiles": poster_profiles,
            "active_niche": niche,
        },
    )


@router.get("/creator/jobs/mine", response_class=HTMLResponse)
async def jobs_mine(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listings = jobs.list_by_poster(session["user_id"])
    return templates.TemplateResponse(
        request, "creator/jobs_mine.html", {"listings": listings}
    )


@router.get("/creator/jobs/new", response_class=HTMLResponse)
async def jobs_new_form(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    return templates.TemplateResponse(
        request,
        "creator/jobs_form.html",
        {
            "listing": {"is_active": True, "listing_type": "collab"},
            "is_new": True,
            "listing_id": None,
            "error": None,
            "vocab": _jobs_vocab(),
        },
    )


@router.post("/creator/jobs")
async def jobs_create(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    form = await request.form()
    payload, error = _validate_listing(form)
    if error:
        return _jobs_form_error(request, form, error, is_new=True, listing_id=None)
    new_id = jobs.create(poster_id=session["user_id"], payload=payload)
    if not new_id:
        return _jobs_form_error(
            request, form, "Couldn't save the posting. Try again.",
            is_new=True, listing_id=None,
        )
    return RedirectResponse(f"/creator/jobs/{new_id}", status_code=303)


@router.get("/creator/jobs/{listing_id}", response_class=HTMLResponse)
async def jobs_detail(
    listing_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listing = jobs.get(listing_id)
    if listing is None or listing.get("is_taken_down"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    is_mine = str(listing["poster_user_id"]) == session["user_id"]
    # Closed postings (is_active=false) shouldn't be discoverable to
    # other creators by guessing the UUID — mirror brand.py's check.
    # The poster still sees their own closed postings so they can
    # re-open them.
    if not is_mine and not listing.get("is_active"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    poster = profiles.get_creator_profile(str(listing["poster_user_id"]))

    # "Apply" CTA logic — for creators, must be connected to the poster.
    can_dm = False
    if not is_mine:
        conn = network.get_connection_between(
            session["user_id"], str(listing["poster_user_id"])
        )
        can_dm = conn is not None and conn.get("status") == "accepted"

    return templates.TemplateResponse(
        request,
        "creator/jobs_detail.html",
        {
            "listing": listing,
            "poster": poster,
            "is_mine": is_mine,
            "can_dm": can_dm,
            "viewer_role": "creator",
        },
    )


@router.get("/creator/jobs/{listing_id}/edit", response_class=HTMLResponse)
async def jobs_edit_form(
    listing_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listing = jobs.get(listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if str(listing["poster_user_id"]) != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return templates.TemplateResponse(
        request,
        "creator/jobs_form.html",
        {
            "listing": listing,
            "is_new": False,
            "listing_id": listing_id,
            "error": None,
            "vocab": _jobs_vocab(),
        },
    )


@router.post("/creator/jobs/{listing_id}")
async def jobs_update(
    listing_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listing = jobs.get(listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if str(listing["poster_user_id"]) != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    form = await request.form()
    payload, error = _validate_listing(form)
    if error:
        return _jobs_form_error(
            request, form, error, is_new=False, listing_id=listing_id
        )
    if not jobs.update(listing_id, payload, poster_id=session["user_id"]):
        return _jobs_form_error(
            request, form, "Couldn't save the posting. Try again.",
            is_new=False, listing_id=listing_id,
        )
    return RedirectResponse(f"/creator/jobs/{listing_id}", status_code=303)


@router.post("/creator/jobs/{listing_id}/close")
async def jobs_close(
    listing_id: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    jobs.deactivate(listing_id, poster_id=session["user_id"])
    return RedirectResponse("/creator/jobs/mine", status_code=303)


@router.post("/creator/jobs/{listing_id}/delete")
async def jobs_delete(
    listing_id: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listing = jobs.get(listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if str(listing["poster_user_id"]) != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if not jobs.delete(listing_id, poster_id=session["user_id"]):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    return RedirectResponse("/creator/jobs", status_code=303)


# -----------------------------------------------------------------------------
# Postings: validation + helpers
# -----------------------------------------------------------------------------

_MONEY_RE = re.compile(r"^\$\s?\d[\d,]*(?:\.\d{2})?$")
_FORBIDDEN_COMPENSATION_TERMS = (
    "negotiable",
    "tfp",
    "trade",
    "product only",
    "product",
)


def _validate_listing(form):
    title = (form.get("title") or "").strip()[:140]
    description = (form.get("description") or "").strip()[:4000]
    listing_type = (form.get("listing_type") or "").strip()
    compensation = (form.get("compensation_text") or "").strip()[:240]
    deadline = (form.get("deadline") or "").strip()[:64]

    if not title:
        return {}, "Please enter a title."
    if not description:
        return {}, "Please enter a description."
    if listing_type not in jobs.LISTING_TYPES:
        return {}, "Pick a posting type."

    compensation_error = _validate_posting_compensation(compensation)
    if compensation_error:
        return {}, compensation_error

    deadline_error = _validate_posting_deadline(deadline)
    if deadline_error:
        return {}, deadline_error

    target_niches: list[str] = []
    seen: set[str] = set()
    from app.routes.onboarding import CREATOR_NICHES
    allowed = set(CREATOR_NICHES)
    for v in form.getlist("target_niches"):
        s = str(v).strip()
        if s in allowed and s not in seen:
            seen.add(s)
            target_niches.append(s)

    payload = {
        "title": title,
        "description": description,
        "listing_type": listing_type,
        "compensation_text": compensation,
        "target_niches": target_niches,
        "deadline": deadline,
        "is_active": True,
    }
    return payload, None


def _validate_posting_compensation(compensation: str) -> str | None:
    if not compensation:
        return "Posting compensation must be a dollar amount."
    lowered = compensation.lower()
    if any(term in lowered for term in _FORBIDDEN_COMPENSATION_TERMS):
        return "Posting compensation must be a dollar amount, like $250."
    if not _MONEY_RE.fullmatch(compensation):
        return "Posting compensation must be a dollar amount, like $250."
    return None


def _validate_posting_deadline(deadline: str) -> str | None:
    if not deadline:
        return "Posting deadline is required."
    try:
        deadline_at = datetime.fromisoformat(deadline)
    except ValueError:
        return "Posting deadline must be a valid date."

    now = datetime.now()
    max_deadline = now + timedelta(days=14)
    if deadline_at <= now:
        return "Posting deadline must be in the future."
    if deadline_at > max_deadline:
        return "Posting deadline must be within 14 days."
    return None


def _jobs_form_error(request, form, message, *, is_new, listing_id):
    listing = {
        "title": form.get("title", ""),
        "description": form.get("description", ""),
        "listing_type": form.get("listing_type", "collab"),
        "compensation_text": form.get("compensation_text", ""),
        "target_niches": list(form.getlist("target_niches")),
        "deadline": form.get("deadline", ""),
        "is_active": True,
    }
    return templates.TemplateResponse(
        request,
        "creator/jobs_form.html",
        {
            "listing": listing,
            "is_new": is_new,
            "listing_id": listing_id,
            "error": message,
            "vocab": _jobs_vocab(),
        },
        status_code=400,
    )


def _jobs_vocab():
    from app.routes.onboarding import CREATOR_NICHES
    return {
        "listing_types": JOB_TYPES,
        "niches": CREATOR_NICHES,
    }


# -----------------------------------------------------------------------------
# Calendar / bookings
# -----------------------------------------------------------------------------


@router.get("/creator/calendar", response_class=HTMLResponse)
async def calendar_list(
    request: Request,
    horizon: str = Query("upcoming"),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if horizon not in ("upcoming", "past", "all"):
        horizon = "upcoming"
    rows = bookings.list_for_user(session["user_id"], horizon=horizon)
    google_connection = oauth_connections.get_google_connection(session["user_id"])
    google_connected = oauth_connections.google_calendar_connected(google_connection)
    return templates.TemplateResponse(
        request,
        "creator/calendar_list.html",
        {
            "bookings": rows,
            "horizon": horizon,
            "google_connected": google_connected,
            "google_configured": google_calendar.is_configured(),
            "calendar_notice": _calendar_notice(request),
        },
    )


@router.get("/creator/google/calendar/connect")
async def google_calendar_connect(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    return RedirectResponse(
        "/creator/google/connect?service=calendar&next=/creator/calendar",
        status_code=302,
    )


@router.get("/creator/google/connect", response_class=HTMLResponse)
async def google_connect_picker(
    request: Request,
    service: str | None = Query(None),
    next_path: str = Query("/creator/profile/settings", alias="next"),
    error: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if not google_calendar.is_configured():
        return RedirectResponse(
            _with_query(
                safe_same_origin(next_path, default="/creator/profile/settings"),
                "google=not_configured",
            ),
            status_code=303,
        )
    selected_services = oauth_connections.normalize_google_services(service or "")
    google_connection = oauth_connections.get_google_connection(session["user_id"])
    return templates.TemplateResponse(
        request,
        "creator/google_connect.html",
        _google_connect_context(
            next_path=next_path,
            selected_services=selected_services,
            google_connection=google_connection,
            error=error,
        ),
    )


@router.post("/creator/google/connect")
async def google_connect_start(
    request: Request,
    calendar: bool = Form(False),
    gmail: bool = Form(False),
    next_path: str = Form("/creator/profile/settings"),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    safe_next = safe_same_origin(next_path, default="/creator/profile/settings")
    selected_services = [
        service
        for service, selected in (
            (oauth_connections.GOOGLE_SERVICE_CALENDAR, calendar),
            (oauth_connections.GOOGLE_SERVICE_GMAIL, gmail),
        )
        if selected
    ]
    if not selected_services:
        google_connection = oauth_connections.get_google_connection(session["user_id"])
        return templates.TemplateResponse(
            request,
            "creator/google_connect.html",
            _google_connect_context(
                next_path=safe_next,
                selected_services=[],
                google_connection=google_connection,
                error="select",
            ),
            status_code=400,
        )
    if not google_calendar.is_configured():
        return RedirectResponse(
            _with_query(safe_next, "google=not_configured"),
            status_code=303,
        )
    selected_scopes = google_calendar.scopes_for_services(selected_services)
    state = oauth_connections.create_google_state(
        session["user_id"],
        services=selected_services,
        scopes=selected_scopes,
        next_path=safe_next,
    )
    try:
        url = google_calendar.auth_url(state, scopes_override=selected_scopes)
    except google_calendar.GoogleCalendarError:
        return RedirectResponse(
            _with_query(safe_next, "google=not_configured"),
            status_code=303,
        )
    return RedirectResponse(url, status_code=302)


@router.get("/creator/google/calendar/callback", name="google_calendar_callback")
@router.get("/auth/google/callback", include_in_schema=False)
@router.get("/oauth/google/callback", include_in_schema=False)
async def google_calendar_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    verified = oauth_connections.verify_google_state(state or "")
    next_path = _google_callback_next(verified)
    if verified and verified["user_id"] != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if error:
        return RedirectResponse(_with_query(next_path, "google=denied"), status_code=303)
    if not code or not verified:
        return RedirectResponse(_with_query(next_path, "google=bad_callback"), status_code=303)

    try:
        token_response = google_calendar.exchange_code(code)
    except google_calendar.GoogleCalendarError:
        return RedirectResponse(
            _with_query(next_path, "google=exchange_failed"), status_code=303
        )
    requested_scopes = google_calendar.allowed_scopes(verified.get("scopes") or [])
    if not requested_scopes:
        requested_scopes = google_calendar.scopes_for_services(
            verified.get("services") or [oauth_connections.GOOGLE_SERVICE_CALENDAR]
        )
    if not oauth_connections.save_google_connection(
        session["user_id"],
        token_response,
        requested_scopes=requested_scopes,
    ):
        return RedirectResponse(_with_query(next_path, "google=save_failed"), status_code=303)

    granted_scopes = _google_effective_callback_scopes(
        token_response,
        requested_scopes=requested_scopes,
    )
    if not google_calendar.has_calendar_scope(granted_scopes):
        return RedirectResponse(_with_query(next_path, "google=connected"), status_code=303)

    result = calendar_sync.sync_google_calendar(session["user_id"])
    if result.error:
        return RedirectResponse(
            _with_query(next_path, "google=connected&sync=failed"),
            status_code=303,
        )
    return RedirectResponse(
        _with_query(next_path, f"google=connected&synced={result.imported}"),
        status_code=303,
    )


@router.post("/creator/google/calendar/sync")
async def google_calendar_sync_now(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    google_connection = oauth_connections.get_google_connection(session["user_id"])
    if not oauth_connections.google_calendar_connected(google_connection):
        return RedirectResponse("/creator/calendar?google=not_connected", status_code=303)
    result = calendar_sync.sync_google_calendar(session["user_id"])
    if result.error == "not_connected":
        return RedirectResponse("/creator/calendar?google=not_connected", status_code=303)
    if result.error:
        return RedirectResponse("/creator/calendar?sync=failed", status_code=303)
    return RedirectResponse(
        f"/creator/calendar?sync=done&synced={result.imported}",
        status_code=303,
    )


@router.post("/creator/google/calendar/disconnect")
async def google_calendar_disconnect(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    oauth_connections.remove_google_service(
        session["user_id"], oauth_connections.GOOGLE_SERVICE_CALENDAR
    )
    return RedirectResponse("/creator/calendar?google=disconnected", status_code=303)


# -----------------------------------------------------------------------------
# Instagram / Meta OAuth
#
# Independent from Google: separate provider row, separate state salt,
# separate scopes. The OAuth scope set itself precludes posting/DMs/
# comments — the service layer doesn't even export a write function.
# After exchange, we resolve the FB Page -> linked IG Business Account.
# Personal IG accounts and unlinked accounts are refused with a
# specific creator-facing message; the connection row is NOT saved.
# -----------------------------------------------------------------------------


def _instagram_next(verified: dict | None, default: str = "/creator/profile/settings") -> str:
    raw = (verified or {}).get("next") or ""
    return safe_same_origin(str(raw), default=default)


@router.get("/creator/instagram/connect")
async def instagram_connect(
    next_path: str = Query("/creator/profile/settings", alias="next"),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    safe_next = safe_same_origin(next_path, default="/creator/profile/settings")
    if not instagram_meta.is_configured():
        return RedirectResponse(
            _with_query(safe_next, "instagram=not_configured"), status_code=303
        )
    state = oauth_connections.create_instagram_state(
        session["user_id"], next_path=safe_next
    )
    try:
        url = instagram_meta.auth_url(state)
    except instagram_meta.InstagramNotConfiguredError:
        return RedirectResponse(
            _with_query(safe_next, "instagram=not_configured"), status_code=303
        )
    return RedirectResponse(url, status_code=302)


@router.get("/creator/instagram/callback", name="instagram_callback")
async def instagram_callback(
    code: str | None = Query(None),
    state: str | None = Query(None),
    error: str | None = Query(None),
    error_reason: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    # The flash hint lands on profile/settings by default; the state
    # carries the real `next` the user came from (onboarding step 4
    # vs settings) so they bounce back to where they started.
    if error or error_reason:
        return RedirectResponse(
            "/creator/profile/settings?instagram=denied", status_code=303
        )
    verified = oauth_connections.verify_instagram_state(state or "")
    if not code or not verified:
        return RedirectResponse(
            "/creator/profile/settings?instagram=bad_callback", status_code=303
        )
    if verified["user_id"] != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)

    safe_next = _instagram_next(verified)

    try:
        token_response = instagram_meta.exchange_code(code)
    except instagram_meta.InstagramNotConfiguredError:
        return RedirectResponse(
            _with_query(safe_next, "instagram=not_configured"), status_code=303
        )
    except instagram_meta.InstagramError:
        return RedirectResponse(
            _with_query(safe_next, "instagram=exchange_failed"), status_code=303
        )

    access_token = str(token_response.get("access_token") or "")
    if not access_token:
        return RedirectResponse(
            _with_query(safe_next, "instagram=exchange_failed"), status_code=303
        )

    # Eligibility resolution — refused for personal IG accounts or
    # accounts not linked to a Facebook Page. Connection row is NOT
    # saved on refusal; the creator gets a specific, actionable
    # message via the integrations grid.
    try:
        ig_account = instagram_meta.resolve_business_account(access_token)
    except instagram_meta.InstagramIneligibleAccountError:
        return RedirectResponse(
            _with_query(safe_next, "instagram=ineligible"), status_code=303
        )
    except instagram_meta.InstagramError:
        return RedirectResponse(
            _with_query(safe_next, "instagram=exchange_failed"), status_code=303
        )

    if not oauth_connections.save_instagram_connection(
        session["user_id"], token_response, ig_account=ig_account
    ):
        return RedirectResponse(
            _with_query(safe_next, "instagram=save_failed"), status_code=303
        )

    return RedirectResponse(
        _with_query(safe_next, "instagram=connected"), status_code=303
    )


@router.post("/creator/instagram/disconnect")
async def instagram_disconnect(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    oauth_connections.disconnect_instagram(session["user_id"])
    return RedirectResponse(
        "/creator/profile/settings?instagram=disconnected", status_code=303
    )


@router.post("/creator/google/gmail/disconnect")
async def google_gmail_disconnect(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    oauth_connections.remove_google_service(
        session["user_id"], oauth_connections.GOOGLE_SERVICE_GMAIL
    )
    return RedirectResponse("/creator/profile/settings?google=disconnected", status_code=303)


@router.post("/creator/profile/delete")
async def profile_delete(
    request: Request,
    confirm: str = Form(""),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    """In-app account deletion.

    Google's Limited Use policy and Meta's platform policy both require
    an in-app path to delete user data (email-only deletion is a common
    OAuth verification blocker). The user must type "delete" into the
    confirm field for the delete to proceed — matches how github,
    google, etc. gate destructive account actions.

    On success we revoke Google, disconnect Instagram, delete the
    `public.users` row (cascades to every downstream table), clear the
    session cookie, and land on a public confirmation page.
    """
    if (confirm or "").strip().lower() != "delete":
        return RedirectResponse(
            "/creator/profile/settings?delete=confirm", status_code=303
        )
    profiles.delete_account(session["user_id"])
    response = RedirectResponse("/?deleted=1", status_code=303)
    clear_session(response)
    clear_pending_role(response)
    return response


@router.get("/creator/calendar/new", response_class=HTMLResponse)
async def calendar_new_form(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    return templates.TemplateResponse(
        request,
        "creator/calendar_form.html",
        {
            "booking": {"type": "event", "status": "confirmed"},
            "is_new": True,
            "booking_id": None,
            "error": None,
            "vocab": {"types": list(bookings.TYPES)},
        },
    )


@router.post("/creator/calendar")
async def calendar_create(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    form = await request.form()
    payload, error = _validate_booking(form)
    if error:
        return _booking_form_error(
            request, form, error, is_new=True, booking_id=None
        )
    new_id = bookings.create(user_id=session["user_id"], payload=payload)
    if not new_id:
        return _booking_form_error(
            request, form, "Couldn't save the booking. Try again.",
            is_new=True, booking_id=None,
        )
    return RedirectResponse(f"/creator/calendar/{new_id}", status_code=303)


@router.get("/creator/calendar/{booking_id}", response_class=HTMLResponse)
async def calendar_detail(
    booking_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    booking = bookings.get(booking_id)
    if booking is None or str(booking["user_id"]) != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request, "creator/calendar_detail.html", {"booking": booking}
    )


@router.get("/creator/calendar/{booking_id}/edit", response_class=HTMLResponse)
async def calendar_edit_form(
    booking_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    booking = bookings.get(booking_id)
    if booking is None or str(booking["user_id"]) != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "creator/calendar_form.html",
        {
            "booking": booking,
            "is_new": False,
            "booking_id": booking_id,
            "error": None,
            "vocab": {"types": list(bookings.TYPES)},
        },
    )


@router.post("/creator/calendar/{booking_id}")
async def calendar_update(
    booking_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    booking = bookings.get(booking_id)
    if booking is None or str(booking["user_id"]) != session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    form = await request.form()
    payload, error = _validate_booking(form)
    if error:
        return _booking_form_error(
            request, form, error, is_new=False, booking_id=booking_id
        )
    if not bookings.update(
        booking_id, user_id=session["user_id"], payload=payload
    ):
        return _booking_form_error(
            request, form, "Couldn't save the booking. Try again.",
            is_new=False, booking_id=booking_id,
        )
    return RedirectResponse(f"/creator/calendar/{booking_id}", status_code=303)


@router.post("/creator/calendar/{booking_id}/cancel")
async def calendar_cancel(
    booking_id: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    bookings.cancel(booking_id, user_id=session["user_id"])
    return RedirectResponse("/creator/calendar", status_code=303)


def _calendar_notice(request: Request) -> str | None:
    params = request.query_params
    google = params.get("google")
    sync = params.get("sync")
    synced = params.get("synced")
    if google == "connected":
        if synced and synced.isdigit():
            count = int(synced)
            return f"Google Calendar connected. {count} item{'s' if count != 1 else ''} synced."
        return "Google Calendar connected."
    if google == "disconnected":
        return "Google Calendar disconnected."
    if google == "not_configured":
        return "Google Calendar needs keys before it can connect."
    if google == "denied":
        return "Google Calendar was not connected."
    if google in {"bad_callback", "exchange_failed", "save_failed"}:
        return "Google Calendar could not connect. try again."
    if google == "not_connected":
        return "connect Google Calendar first."
    if sync == "done":
        if synced and synced.isdigit():
            count = int(synced)
            return f"calendar synced. {count} item{'s' if count != 1 else ''} refreshed."
        return "calendar synced."
    if sync == "failed":
        return "calendar sync failed. try again."
    return None


def _with_query(path: str, query: str) -> str:
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{query}"


def _google_connect_context(
    *,
    next_path: str,
    selected_services: list[str],
    google_connection: dict | None,
    error: str | None,
) -> dict:
    return {
        "selected_services": selected_services,
        "next_path": safe_same_origin(next_path, default="/creator/profile/settings"),
        "error": error,
        "google_calendar_connected": oauth_connections.google_calendar_connected(
            google_connection
        ),
        "google_gmail_connected": oauth_connections.google_gmail_connected(
            google_connection
        ),
        "google_service_scopes": {
            oauth_connections.GOOGLE_SERVICE_CALENDAR: oauth_connections.google_service_scopes(
                oauth_connections.GOOGLE_SERVICE_CALENDAR
            ),
            oauth_connections.GOOGLE_SERVICE_GMAIL: oauth_connections.google_service_scopes(
                oauth_connections.GOOGLE_SERVICE_GMAIL
            ),
        },
    }


def _google_callback_next(
    verified: dict | None, default: str = "/creator/profile/settings"
) -> str:
    raw = (verified or {}).get("next") or ""
    return safe_same_origin(str(raw), default=default)


def _google_effective_callback_scopes(
    token_response: dict,
    *,
    requested_scopes: list[str],
) -> list[str]:
    explicit = str(token_response.get("scope") or "").replace(",", " ").split()
    return google_calendar.allowed_scopes([scope for scope in explicit if scope]) or requested_scopes


# -----------------------------------------------------------------------------
# Booking validation
# -----------------------------------------------------------------------------


def _validate_booking(form):
    title = (form.get("title") or "").strip()[:140]
    starts_at = (form.get("starts_at") or "").strip()[:64]
    ends_at = (form.get("ends_at") or "").strip()[:64]
    btype = (form.get("type") or "").strip()
    notes = (form.get("notes") or "").strip()[:2000]
    venue_name = (form.get("venue_name") or "").strip()[:160]
    bstatus = (form.get("status") or "confirmed").strip()

    if not title:
        return {}, "Please enter a title."
    if not starts_at:
        return {}, "Please set a start time."
    if btype not in bookings.TYPES:
        return {}, "Pick a type."
    if bstatus not in bookings.STATUSES:
        bstatus = "confirmed"

    payload = {
        "title": title,
        "type": btype,
        "starts_at": starts_at,
        "ends_at": ends_at or None,
        "notes": notes or None,
        "venue_name": venue_name or None,
        "status": bstatus,
    }
    return payload, None


def _booking_form_error(request, form, message, *, is_new, booking_id):
    booking = {
        "title": form.get("title", ""),
        "type": form.get("type", "event"),
        "starts_at": form.get("starts_at", ""),
        "ends_at": form.get("ends_at", ""),
        "notes": form.get("notes", ""),
        "venue_name": form.get("venue_name", ""),
        "status": form.get("status", "confirmed"),
    }
    return templates.TemplateResponse(
        request,
        "creator/calendar_form.html",
        {
            "booking": booking,
            "is_new": is_new,
            "booking_id": booking_id,
            "error": message,
            "vocab": {"types": list(bookings.TYPES)},
        },
        status_code=400,
    )


# -----------------------------------------------------------------------------
# Content receipts
# -----------------------------------------------------------------------------


@router.get("/creator/receipts", response_class=HTMLResponse)
async def receipts_list(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    rows = receipts.list_for_user(session["user_id"])
    return templates.TemplateResponse(
        request, "creator/receipts_list.html", {"receipts": rows}
    )


@router.get("/creator/receipts/new", response_class=HTMLResponse)
async def receipts_new_form(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    return templates.TemplateResponse(
        request,
        "creator/receipts_form.html",
        {
            "receipt": {"post_type": "reel"},
            "error": None,
            "vocab": {"post_types": list(receipts.POST_TYPES)},
        },
    )


@router.post("/creator/receipts")
async def receipts_create(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    form = await request.form()
    payload, error = _validate_receipt(form)
    if error:
        return templates.TemplateResponse(
            request,
            "creator/receipts_form.html",
            {
                "receipt": {
                    "post_url": form.get("post_url", ""),
                    "post_type": form.get("post_type", "reel"),
                    "caption_excerpt": form.get("caption_excerpt", ""),
                    "likes_count": form.get("likes_count", ""),
                    "comments_count": form.get("comments_count", ""),
                    "posted_at": form.get("posted_at", ""),
                },
                "error": error,
                "vocab": {"post_types": list(receipts.POST_TYPES)},
            },
            status_code=400,
        )
    if not receipts.create(user_id=session["user_id"], payload=payload):
        return templates.TemplateResponse(
            request,
            "creator/receipts_form.html",
            {
                "receipt": payload,
                "error": "Couldn't save the receipt. Try again.",
                "vocab": {"post_types": list(receipts.POST_TYPES)},
            },
            status_code=400,
        )
    return RedirectResponse("/creator/receipts", status_code=303)


# -----------------------------------------------------------------------------
# Performance
# -----------------------------------------------------------------------------


@router.get("/creator/performance", response_class=HTMLResponse)
async def performance_list(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    user_id = session["user_id"]
    view = stats_merge.performance_view(user_id)
    # `instagram_status` is the single source of truth for the
    # page-foot copy + the "temporarily unavailable" banner.
    return templates.TemplateResponse(
        request,
        "creator/performance_list.html",
        {
            "rows": view.rows,
            "instagram_status": view.instagram_status,
        },
    )

# -----------------------------------------------------------------------------
# Validation helpers for receipts
# -----------------------------------------------------------------------------


def _validate_receipt(form):
    post_url = (form.get("post_url") or "").strip()[:500]
    post_type = (form.get("post_type") or "").strip()
    caption = (form.get("caption_excerpt") or "").strip()[:500]
    posted_at = (form.get("posted_at") or "").strip()[:64]
    likes_raw = (form.get("likes_count") or "").strip()
    comments_raw = (form.get("comments_count") or "").strip()

    if not post_url:
        return {}, "Please paste the post URL."
    safe_post_url = http_url_or_none(post_url)
    if safe_post_url is None:
        return {}, "Post URL must be a valid http(s) URL."
    post_url = safe_post_url
    if post_type not in receipts.POST_TYPES:
        return {}, "Pick a post type."
    likes = _maybe_int(likes_raw)
    comments = _maybe_int(comments_raw)
    if likes_raw and likes is None:
        return {}, "Likes must be a whole number."
    if comments_raw and comments is None:
        return {}, "Comments must be a whole number."

    payload = {
        "post_url": post_url,
        "post_type": post_type,
        "caption_excerpt": caption or None,
        "likes_count": likes,
        "comments_count": comments,
        "posted_at": posted_at or None,
    }
    return payload, None


def _maybe_int(s: str):
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None
