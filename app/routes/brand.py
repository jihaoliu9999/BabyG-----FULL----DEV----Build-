"""Brand entry routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import discover, dms, jobs, network, notifications, profiles

router = APIRouter(prefix="/brand", tags=["brand"])

# Closed vocabularies used by the brand profile + campaign forms. Kept
# here (not in the service layer) because they're shaped specifically
# for the brand UI; the service layer takes whatever the route hands it.
BRAND_INDUSTRIES = (
    "fashion", "beauty", "fitness", "food", "travel", "tech",
    "lifestyle", "music", "gaming", "nightlife", "wellness", "other",
)
BRAND_CAMPAIGN_TYPES = (
    "ugc", "paid_post", "event_appearance", "long_form", "barter",
)
BRAND_CREATOR_SIZES = (
    "nano", "micro", "mid", "macro", "mega",
)
BRAND_BUDGET_RANGES = (
    "under_1k", "1k_5k", "5k_25k", "25k_plus",
)


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    """Brand dashboard: profile completion, real activity counts, quick
    actions. Honest empty states everywhere — no fake stats, no fake
    messages, no fake verified badges. Each tile links to the actual
    surface it summarizes so a user can dig in."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    counts = _dashboard_counts(session["user_id"])
    completion = _profile_completion(profile)
    return templates.TemplateResponse(
        request,
        "brand/dashboard.html",
        {
            "profile": profile,
            "completion": completion,
            "counts": counts,
        },
    )


# ---------------------------------------------------------------------------
# Profile
# ---------------------------------------------------------------------------


@router.get("/profile", response_class=HTMLResponse)
async def profile_page(
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    return templates.TemplateResponse(
        request,
        "brand/profile.html",
        {
            "profile": profile,
            "industries": BRAND_INDUSTRIES,
            "campaign_types": BRAND_CAMPAIGN_TYPES,
            "creator_sizes": BRAND_CREATOR_SIZES,
            "budget_ranges": BRAND_BUDGET_RANGES,
        },
    )


@router.post("/profile/identity")
async def profile_identity_update(
    company_name: str = Form(""),
    brand_website: str = Form(""),
    industry: str = Form(""),
    contact_full_name: str = Form(""),
    contact_title: str = Form(""),
    product_description: str = Form(""),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Identity section: company, website, industry, contact, description.
    Updates only via a closed allow-list — no field can be set by adding
    an extra POST field. Empty strings clear the column (via NULL) so a
    brand can blank out an earlier entry."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    payload: dict[str, Any] = {}
    name = _normalize(company_name, 120)
    if name:
        payload["company_name"] = name
    website = _normalize(brand_website, 200)
    payload["brand_website"] = website or None
    ind = industry.strip().lower()
    payload["industry"] = ind if ind in BRAND_INDUSTRIES else None
    contact = _normalize(contact_full_name, 120)
    if contact:
        payload["contact_full_name"] = contact
    payload["contact_title"] = _normalize(contact_title, 120) or None
    payload["product_description"] = _normalize(product_description, 600) or None
    if not profiles.update_brand_profile(session["user_id"], payload):
        return RedirectResponse(
            "/brand/profile?identity=save_failed", status_code=303
        )
    return RedirectResponse("/brand/profile?identity=ok", status_code=303)


@router.post("/profile/preferences")
async def profile_preferences_update(
    request: Request,
    budget_range: str = Form(""),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Working terms: campaign types, creator size targeting, niche
    interests, budget range. The chip groups arrive as repeated form
    fields — read via getlist()."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    form = await request.form()
    campaign_types = _clean_chip_list(
        form.getlist("campaign_types"), BRAND_CAMPAIGN_TYPES
    )
    creator_sizes = _clean_chip_list(
        form.getlist("creator_size_preferences"), BRAND_CREATOR_SIZES
    )
    # Niches reuse the creator-side niche vocab (lifestyle, nightlife, …)
    # but we accept whatever the chip group ships — the column is text[]
    # and Discover ranking does string-match-only.
    niches = _clean_chip_list(form.getlist("niche_preferences"), None, max_len=12)

    payload: dict[str, Any] = {
        "campaign_types": campaign_types,
        "creator_size_preferences": creator_sizes,
        "niche_preferences": niches,
    }
    br = budget_range.strip().lower()
    payload["budget_range"] = br if br in BRAND_BUDGET_RANGES else None

    if not profiles.update_brand_profile(session["user_id"], payload):
        return RedirectResponse(
            "/brand/profile?preferences=save_failed", status_code=303
        )
    return RedirectResponse("/brand/profile?preferences=ok", status_code=303)


# ---------------------------------------------------------------------------
# Campaigns (reuses creator_job_listings with listing_type='brand_deal')
# ---------------------------------------------------------------------------


@router.get("/campaigns", response_class=HTMLResponse)
async def campaigns_list(
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    listings = jobs.list_by_poster(session["user_id"])
    return templates.TemplateResponse(
        request,
        "brand/campaigns_list.html",
        {"profile": profile, "listings": listings},
    )


@router.get("/campaigns/new", response_class=HTMLResponse)
async def campaigns_new_form(
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    return templates.TemplateResponse(
        request,
        "brand/campaigns_new.html",
        {
            "profile": profile,
            "niches_default": list(profile.get("niche_preferences") or []),
            "error": None,
        },
    )


@router.post("/campaigns")
async def campaigns_create(
    request: Request,
    title: str = Form(""),
    description: str = Form(""),
    compensation_text: str = Form(""),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    form = await request.form()
    title_clean = _normalize(title, 120)
    description_clean = _normalize(description, 2000)
    if not title_clean or not description_clean:
        return templates.TemplateResponse(
            request,
            "brand/campaigns_new.html",
            {
                "profile": profile,
                "niches_default": list(profile.get("niche_preferences") or []),
                "error": "title and description are required.",
            },
            status_code=400,
        )

    target_niches = _clean_chip_list(form.getlist("target_niches"), None, max_len=12)
    payload = {
        "title": title_clean,
        "description": description_clean,
        # Brand-posted listings flow into the same Discover pipeline as
        # creator listings; the listing_type discriminator lets future
        # filters surface "brand deals only" without a new table.
        "listing_type": "brand_deal",
        "compensation_text": _normalize(compensation_text, 200) or None,
        "target_niches": target_niches,
        "is_active": True,
        "is_taken_down": False,
    }
    listing_id = jobs.create(poster_id=session["user_id"], payload=payload)
    if not listing_id:
        return templates.TemplateResponse(
            request,
            "brand/campaigns_new.html",
            {
                "profile": profile,
                "niches_default": list(profile.get("niche_preferences") or []),
                "error": "couldn't save the campaign. try again.",
            },
            status_code=400,
        )
    return RedirectResponse("/brand/campaigns?created=ok", status_code=303)


# ---------------------------------------------------------------------------
# Saved + DM placeholders
# ---------------------------------------------------------------------------


@router.get("/saved", response_class=HTMLResponse)
async def saved_page(
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Creators the brand has saved from Discover. Backed by
    `creator_discovery_actions(action_type='saved')` — already supported
    by migration 0021. Empty state when no saves yet, honest, no fakes."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    saved_creators = _list_saved_creators(session["user_id"])
    return templates.TemplateResponse(
        request,
        "brand/saved.html",
        {"profile": profile, "saved_creators": saved_creators},
    )


@router.get("/dm", response_class=HTMLResponse)
async def dm_page(
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Brand messaging placeholder. The DM service supports brand users
    at the schema level — `dm_threads(participant_a_id, participant_b_id)`
    accepts any two user_ids — but a real brand-to-creator inbox UI is
    deferred to the Phase 5 brand outreach work. For now we render a
    polished empty state inside the same shell, with the existing thread
    count surfaced if any threads do exist (e.g. from connection
    requests that auto-opened one)."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    thread_count = len(dms.list_threads_for_user(session["user_id"]))
    return templates.TemplateResponse(
        request,
        "brand/dm.html",
        {"profile": profile, "thread_count": thread_count},
    )


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(
    request: Request,
    kind: str = Query("creator"),
    category: str | None = Query(None),
    location: str | None = Query(None),
    budget_min: int | None = Query(None, ge=0),
    budget_max: int | None = Query(None, ge=0),
    bring_back_kind: str | None = Query(None),
    bring_back_id: str | None = Query(None),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    kind_clean = _brand_discover_kind(kind)
    prioritize = None
    if bring_back_kind and bring_back_id:
        prioritize = (_brand_discover_kind(bring_back_kind), bring_back_id)
    cards = _brand_discover_cards(
        viewer_id=session["user_id"],
        kind=kind_clean,
        category=category,
        location=location,
        budget_min=budget_min,
        budget_max=budget_max,
        viewer_tags=list(profile.get("niche_preferences") or []),
        prioritize=prioritize,
    )
    if cards:
        top = cards[0]
        discover.record_action(
            user_id=session["user_id"],
            target_kind=top["card_kind"],
            target_card_id=top["card_id"],
            target_user_id=top["owner_user_id"],
            action_type="viewed",
        )
    return templates.TemplateResponse(
        request,
        "creator/discover.html",
        {
            "profile": profile,
            "cards": cards,
            "active_kind": kind_clean,
            "discover_tabs": [
                ("creator", "people"),
                ("opportunity", "opportunities"),
            ],
            "category": category or "",
            "location": location or "",
            "budget_min": budget_min,
            "budget_max": budget_max,
            "can_undo": discover.last_undoable_pass(session["user_id"]) is not None,
            "discover_base_path": "/brand/discover",
            "discover_swipe_path": "/brand/discover/swipe",
            "discover_undo_path": "/brand/discover/undo",
            "discover_title": "discover",
            "discover_subtitle": "creators and opportunities worth knowing.",
        },
    )


@router.post("/discover/swipe")
async def discover_swipe(
    target_kind: str = Form(...),
    target_card_id: str = Form(...),
    action: str = Form(...),
    kind: str = Form("creator"),
    category: str = Form(""),
    location: str = Form(""),
    budget_min: int | None = Form(None, ge=0),
    budget_max: int | None = Form(None, ge=0),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    action_clean = str(action or "").strip().lower()
    if action_clean not in {"passed", "saved", "connected", "interested", "opened_profile"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    target_kind_clean = _brand_discover_kind(target_kind)
    card = discover.get_card(card_kind=target_kind_clean, card_id=target_card_id)
    if card is None or card["owner_user_id"] == session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if card["card_kind"] not in {"creator", "opportunity"}:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    expected_primary = "interested" if card["card_kind"] == "opportunity" else "connected"
    if action_clean in {"connected", "interested"} and action_clean != expected_primary:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    if not discover.record_action(
        user_id=session["user_id"],
        target_kind=card["card_kind"],
        target_card_id=card["card_id"],
        target_user_id=card["owner_user_id"],
        action_type=action_clean,
    ):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    if action_clean in {"connected", "interested"} and network.request_connection(
        requester_id=session["user_id"], addressee_id=card["owner_user_id"]
    ):
        notifications.create(
            user_id=card["owner_user_id"],
            kind="connection_request",
            title="Someone wants to connect.",
            body=None,
            link_path="/creator/connections",
        )
    if action_clean == "opened_profile":
        return RedirectResponse(_brand_detail_path(card), status_code=303)
    return RedirectResponse(
        _discover_url(kind, category, location, budget_min, budget_max), status_code=303
    )


@router.post("/discover/undo")
async def discover_undo(
    kind: str = Form("creator"),
    category: str = Form(""),
    location: str = Form(""),
    budget_min: int | None = Form(None, ge=0),
    budget_max: int | None = Form(None, ge=0),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    previous = discover.last_undoable_pass(session["user_id"])
    if previous is None:
        return RedirectResponse(
            _discover_url(kind, category, location, budget_min, budget_max), 303
        )
    target_kind, target_card_id = previous
    target_kind = _brand_discover_kind(target_kind)
    card = discover.get_card(card_kind=target_kind, card_id=target_card_id)
    discover.record_action(
        user_id=session["user_id"],
        target_kind=target_kind,
        target_card_id=target_card_id,
        target_user_id=card["owner_user_id"] if card else None,
        action_type="undo_pass",
    )
    params = {
        "kind": _brand_discover_kind(kind),
        "bring_back_kind": target_kind,
        "bring_back_id": target_card_id,
    }
    if category:
        params["category"] = category
    if location:
        params["location"] = location
    if budget_min is not None:
        params["budget_min"] = str(budget_min)
    if budget_max is not None:
        params["budget_max"] = str(budget_max)
    return RedirectResponse(f"/brand/discover?{urlencode(params)}", status_code=303)


@router.get("/discover/creator/{creator_user_id}", response_class=HTMLResponse)
async def discover_creator_detail(
    creator_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    card = discover.get_card(card_kind="creator", card_id=creator_user_id)
    if card is None or card["owner_user_id"] == session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "brand/discover_detail.html",
        {"profile": profile, "card": _brand_card(card)},
    )


@router.get("/discover/opportunity/{opportunity_id}", response_class=HTMLResponse)
async def discover_opportunity_detail(
    opportunity_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    card = discover.get_card(card_kind="opportunity", card_id=opportunity_id)
    if card is None or card["owner_user_id"] == session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "brand/discover_detail.html",
        {"profile": profile, "card": _brand_card(card)},
    )


def _dashboard_counts(brand_user_id: str) -> dict[str, int]:
    """Counts for the four dashboard tiles. Returns real numbers from
    real tables — defaults to zero on any service failure so a flaky
    Supabase doesn't blank the whole dashboard."""
    try:
        active_campaigns = sum(
            1 for r in jobs.list_by_poster(brand_user_id)
            if r.get("is_active") and not r.get("is_taken_down")
        )
    except Exception:
        active_campaigns = 0
    try:
        saved = len(_list_saved_creators(brand_user_id))
    except Exception:
        saved = 0
    try:
        inbound = len(network.list_incoming_pending(brand_user_id))
    except Exception:
        inbound = 0
    try:
        unread_dms = dms.unread_count_for_user(brand_user_id)
    except Exception:
        unread_dms = 0
    return {
        "active_campaigns": active_campaigns,
        "saved": saved,
        "inbound_interest": inbound,
        "unread_dms": unread_dms,
    }


# Fields used to drive the dashboard's profile-completion meter. Tracks
# the same shape onboarding requires plus the two optional polish fields
# (logo + product description) so a fully-filled profile reaches 100%.
_BRAND_COMPLETION_FIELDS: tuple[str, ...] = (
    "company_name",
    "brand_website",
    "industry",
    "contact_full_name",
    "logo_url",
    "product_description",
    "campaign_types",
    "creator_size_preferences",
    "niche_preferences",
    "budget_range",
)


def _profile_completion(profile: dict[str, Any]) -> dict[str, Any]:
    """Returns ``{percent, filled, missing}`` so the dashboard card can
    render a completion meter + a short list of what's still empty.
    Array fields count as filled when they have at least one entry."""
    filled: list[str] = []
    missing: list[str] = []
    for f in _BRAND_COMPLETION_FIELDS:
        value = profile.get(f)
        if isinstance(value, list):
            (filled if value else missing).append(f)
        else:
            (filled if (value or "").strip() else missing).append(f)
    percent = round(100 * len(filled) / len(_BRAND_COMPLETION_FIELDS))
    return {"percent": percent, "filled": filled, "missing": missing}


def _list_saved_creators(brand_user_id: str) -> list[dict[str, Any]]:
    """Creators this brand has saved from Discover. Reads
    `creator_discovery_actions(action_type='saved')` (migration 0021
    extended the vocabulary). Returns the public-projected view of
    each saved creator — never owner-private fields."""
    try:
        from app.core import supabase_client
        result = (
            supabase_client.get_service_client()
            .table("creator_discovery_actions")
            .select("target_user_id, created_at")
            .eq("user_id", brand_user_id)
            .eq("action_type", "saved")
            .eq("target_kind", "creator")
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
    except Exception:
        return []
    rows = getattr(result, "data", None) or []
    target_ids = [r["target_user_id"] for r in rows if r.get("target_user_id")]
    if not target_ids:
        return []
    public_views = profiles.get_creators_by_ids(target_ids)
    # Preserve "newest save first" order from the actions query.
    return [public_views[t] for t in target_ids if t in public_views]


def _normalize(value: str, max_len: int) -> str:
    return " ".join((value or "").split())[:max_len]


def _clean_chip_list(
    raw: list[str], allowed: tuple[str, ...] | None, *, max_len: int = 12
) -> list[str]:
    """Lowercase, dedupe, optionally filter to a closed allow-list."""
    out: list[str] = []
    seen: set[str] = set()
    for value in raw or []:
        v = (value or "").strip().lower()
        if not v or v in seen:
            continue
        if allowed is not None and v not in allowed:
            continue
        seen.add(v)
        out.append(v)
        if len(out) >= max_len:
            break
    return out


def _brand_discover_kind(value: str | None) -> str:
    kind = discover.clean_kind(value)
    return kind if kind in {"creator", "opportunity"} else "creator"


def _brand_discover_cards(
    *,
    viewer_id: str,
    kind: str,
    category: str | None,
    location: str | None,
    budget_min: int | None,
    budget_max: int | None,
    viewer_tags: list[str],
    prioritize: tuple[str, str] | None,
) -> list[dict]:
    cards = discover.list_cards(
        viewer_id=viewer_id,
        viewer_role="brand",
        kind=kind,
        category=category,
        location=location,
        budget_min=budget_min,
        budget_max=budget_max,
        viewer_tags=viewer_tags,
        prioritize=prioritize,
    )
    return [_brand_card(card) for card in cards if card["card_kind"] in {"creator", "opportunity"}]


def _brand_card(card: dict) -> dict:
    card = dict(card)
    card["detail_path"] = _brand_detail_path(card)
    return card


def _brand_detail_path(card: dict) -> str:
    if card["card_kind"] == "opportunity":
        return f"/brand/discover/opportunity/{card['card_id']}"
    return f"/brand/discover/creator/{card['card_id']}"


def _discover_url(
    kind: str,
    category: str,
    location: str,
    budget_min: int | None = None,
    budget_max: int | None = None,
) -> str:
    params = {"kind": _brand_discover_kind(kind)}
    if category:
        params["category"] = category
    if location:
        params["location"] = location
    if budget_min is not None:
        params["budget_min"] = str(budget_min)
    if budget_max is not None:
        params["budget_max"] = str(budget_max)
    return f"/brand/discover?{urlencode(params)}"
