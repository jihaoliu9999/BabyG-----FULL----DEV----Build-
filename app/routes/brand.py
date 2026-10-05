"""Brand entry routes."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import (
    discover,
    dms,
    job_applications,
    job_deals,
    job_offers,
    jobs,
    my_opportunities,
    network,
    notifications,
    profiles,
)

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
    # Starlette's FormData.getlist() is typed as list[UploadFile | str]
    # because the same FormData carries file uploads on multipart forms.
    # Chip groups never accept files; filter to strings at the boundary
    # so the chip-cleaner signature stays narrow.
    campaign_types = _clean_chip_list(
        _form_strings(form, "campaign_types"), BRAND_CAMPAIGN_TYPES
    )
    creator_sizes = _clean_chip_list(
        _form_strings(form, "creator_size_preferences"), BRAND_CREATOR_SIZES
    )
    # Niches reuse the creator-side niche vocab (lifestyle, nightlife, …)
    # but we accept whatever the chip group ships — the column is text[]
    # and Discover ranking does string-match-only.
    niches = _clean_chip_list(
        _form_strings(form, "niche_preferences"), None, max_len=12
    )

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

    target_niches = _clean_chip_list(
        _form_strings(form, "target_niches"), None, max_len=12
    )
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
    listing_id = jobs.create(
        poster_id=session["user_id"], poster_role=session["role"], payload=payload
    )
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
    view: str = Query("messages"),
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
    # Step 6C: "messages | deals". Messages stays the existing placeholder;
    # Deals lists the deals this user is a party to (same rows as creators).
    if str(view or "").strip().lower() == "deals":
        deals = job_deals.list_for_user(session["user_id"])
        return templates.TemplateResponse(
            request,
            "brand/dm.html",
            {
                "profile": profile,
                "dm_view": "deals",
                "deals": deals or [],
                "deals_failed": deals is None,
                "deal_base": "/brand/dm/deals/",
            },
        )
    thread_count = len(dms.list_threads_for_user(session["user_id"]))
    return templates.TemplateResponse(
        request,
        "brand/dm.html",
        {"profile": profile, "thread_count": thread_count, "dm_view": "messages"},
    )


@router.get("/dm/deals/{deal_id}", response_class=HTMLResponse)
async def dm_deal_detail(
    deal_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Step 6C: one deal, for either of its two parties (relationship, not
    role). Anyone else gets the same 404 as a missing id."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    deal = job_deals.get_for_user(deal_id, session["user_id"])
    if deal is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    listing = jobs.get(str(deal["listing_id"]))
    opportunity_path = (
        f"/brand/discover/opportunity/{deal['listing_id']}"
        if listing is not None and jobs.can_view_detail(listing, session["user_id"])
        else None
    )
    return templates.TemplateResponse(
        request,
        "creator/deal_detail.html",
        {
            "profile": profile,
            "deal": deal,
            "back_path": "/brand/dm?view=deals",
            "opportunity_path": opportunity_path,
            "babyg_state": job_deals.BABYG_STATE,
        },
    )


@router.get("/discover", response_class=HTMLResponse)
async def discover_page(
    request: Request,
    kind: str = Query("all"),
    category: str | None = Query(None),
    location: str | None = Query(None),
    budget_min: int | None = Query(None, ge=0),
    budget_max: int | None = Query(None, ge=0),
    bring_back_kind: str | None = Query(None),
    bring_back_id: str | None = Query(None),
    view: str = Query("explore"),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    kind_clean = _brand_discover_kind(kind)
    # Step 5C: "my opportunities" is a secondary view of the
    # Opportunities tab only. Anywhere else, `view` is ignored.
    active_view = (
        my_opportunities.clean_view(view)
        if kind_clean == "opportunity"
        else my_opportunities.VIEW_EXPLORE
    )
    if active_view == my_opportunities.VIEW_MINE:
        # Personal list: listings owned by the AUTHENTICATED user
        # (creator_job_listings.poster_user_id), each with a real
        # applicant count. No Explore feed work and no "viewed" write.
        fallback_location = ", ".join(
            p for p in (profile.get("location_city"), profile.get("location_region")) if p
        ) or None
        mine_cards = my_opportunities.apply_filters(
            my_opportunities.poster_items(
                session["user_id"],
                detail_prefix="/brand/discover/opportunity/",
                detail_suffix="/applicants",
                fallback_location=fallback_location,
            ),
            category=category,
            location=location,
            budget_min=budget_min,
            budget_max=budget_max,
        )
        return templates.TemplateResponse(
            request,
            "creator/discover.html",
            {
                "profile": profile,
                "cards": mine_cards,
                "active_kind": kind_clean,
                "active_view": active_view,
                "mine_role": "brand",
                "category": category or "",
                "location": location or "",
                "budget_min": budget_min,
                "budget_max": budget_max,
                "can_undo": False,
                "discover_base_path": "/brand/discover",
                "discover_swipe_path": "/brand/discover/swipe",
                "discover_undo_path": "/brand/discover/undo",
                "discover_post_path": "/brand/campaigns/new",
                "discover_title": "discover",
            },
        )
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
            "active_view": active_view,
            "category": category or "",
            "location": location or "",
            "budget_min": budget_min,
            "budget_max": budget_max,
            "can_undo": discover.last_undoable_pass(session["user_id"]) is not None,
            "discover_base_path": "/brand/discover",
            "discover_swipe_path": "/brand/discover/swipe",
            "discover_undo_path": "/brand/discover/undo",
            "discover_post_path": "/brand/campaigns/new",
            "discover_title": "discover",
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
    if card["card_kind"] not in {"creator", "brand", "opportunity"}:
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
    listing = jobs.get(opportunity_id)
    if listing is None or not jobs.can_view_detail(listing, session["user_id"]):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    is_mine = str(listing.get("poster_user_id")) == session["user_id"]
    poster_id = str(listing["poster_user_id"])
    if listing.get("poster_role") == "brand":
        poster = profiles.public_brand(profiles.get_brand_profile(poster_id))
    else:
        poster = profiles.public_creator(profiles.get_creator_profile(poster_id))
    return templates.TemplateResponse(
        request,
        "creator/jobs_detail.html",
        {
            "profile": profile,
            "listing": listing,
            "poster": poster,
            "is_mine": is_mine,
            "can_dm": False,
            "viewer_role": "brand",
            "back_path": "/brand/discover?kind=opportunity",
            # Step 5B: brand viewers never apply — the Apply affordance
            # is creator-only. Passing explicit False keeps the template
            # contract uniform between the two routes that reuse it.
            "already_applied": False,
        },
    )


def _review_listing_or_raise(listing_id: str, user_id: str) -> dict[str, Any]:
    """Step 5D gate shared by both applicant-review handlers: only the
    poster (``poster_user_id`` == the authenticated session user) gets the
    listing back. Missing/hidden -> 404, someone else's -> 403."""
    access, listing = job_applications.authorize_poster(listing_id, user_id)
    if access == job_applications.REVIEW_FORBIDDEN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    if access != job_applications.REVIEW_OK or listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return listing


@router.get(
    "/discover/opportunity/{opportunity_id}/applicants",
    response_class=HTMLResponse,
)
async def discover_opportunity_applicants(
    opportunity_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Step 5D: applicants to an opportunity the viewer POSTED.

    Review only. Access is the listing's ``poster_user_id`` matching the
    session user, enforced server-side; the list carries no application
    messages."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    listing = _review_listing_or_raise(opportunity_id, session["user_id"])
    rows = job_applications.list_for_poster(listing, session["user_id"])
    return templates.TemplateResponse(
        request,
        "creator/opportunity_applicants.html",
        {
            "profile": profile,
            "listing": listing,
            "applicants": job_applications.attach_applicants(rows) if rows else [],
            "load_failed": rows is None,
            "applicants_path": f"/brand/discover/opportunity/{listing['id']}/applicants",
            "back_path": "/brand/discover?kind=opportunity&view=mine",
        },
        status_code=503 if rows is None else 200,
    )


@router.get(
    "/discover/opportunity/{opportunity_id}/applicants/{application_id}",
    response_class=HTMLResponse,
)
async def discover_opportunity_application(
    opportunity_id: str,
    application_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Step 5D: one application to an opportunity the viewer POSTED. The
    application must belong to THIS listing and the listing to the session
    user (re-proved in ``get_for_poster``); otherwise 404/403 and no
    message is read into the response."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    listing = _review_listing_or_raise(opportunity_id, session["user_id"])
    row = job_applications.get_for_poster(listing, application_id, session["user_id"])
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    application = job_applications.attach_applicants([row])[0]
    applicant = application["applicant"]
    # The EXISTING brand-side creator profile route. It resolves through
    # the discovery card (404 when the creator has none), so the link is
    # only offered when that route will actually open.
    profile_href = None
    if (
        applicant["found"]
        and applicant["onboarded"]
        and discover.get_card(card_kind="creator", card_id=applicant["user_id"])
    ):
        profile_href = f"/brand/discover/creator/{applicant['user_id']}"
    # Step 6A: one offer per application. "Offer sent" replaces "Make offer"
    # once it exists; never offered on a self-relationship.
    application_path = (
        f"/brand/discover/opportunity/{listing['id']}/applicants/{application['id']}"
    )
    offer = job_offers.get_for_application(application["id"], session["user_id"])
    # Step 6C: once the offer is accepted, the poster's path is its deal.
    deal_id = (
        job_deals.deal_id_for_offer(str(offer["id"]), session["user_id"])
        if offer and offer.get("status") == job_offers.STATUS_ACCEPTED
        else None
    )
    return templates.TemplateResponse(
        request,
        "creator/opportunity_application.html",
        {
            "profile": profile,
            "listing": listing,
            "application": application,
            "applicant": applicant,
            "profile_href": profile_href,
            "applicants_path": f"/brand/discover/opportunity/{listing['id']}/applicants",
            "offer": offer,
            "deal_path": f"/brand/dm/deals/{deal_id}" if deal_id else None,
            "offer_path": (
                f"{application_path}/offer"
                if job_offers.can_offer(application, session["user_id"])
                else None
            ),
        },
    )


def _offer_target_or_raise(
    listing_id: str, application_id: str, user_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Step 6A: the listing the session user POSTED plus one application to
    it -- the same chain as the Application page -- and never a
    self-relationship. 404 missing/mismatched, 403 not the poster / self."""
    listing = _review_listing_or_raise(listing_id, user_id)
    application = job_applications.get_for_poster(listing, application_id, user_id)
    if application is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if not job_offers.can_offer(application, user_id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return listing, application


def _offer_form_response(
    request: Request,
    profile: dict[str, Any],
    listing: dict[str, Any],
    application: dict[str, Any],
    *,
    values: dict[str, str],
    errors: dict[str, str],
    form_error: str | None = None,
    status_code: int = 200,
) -> Response:
    application_path = (
        f"/brand/discover/opportunity/{listing['id']}/applicants/{application['id']}"
    )
    return templates.TemplateResponse(
        request,
        "creator/opportunity_offer.html",
        {
            "profile": profile,
            "listing": listing,
            "applicant": job_applications.attach_applicants([application])[0]["applicant"],
            "application_path": application_path,
            "offer_path": f"{application_path}/offer",
            "values": values,
            "errors": errors,
            "form_error": form_error,
            "min_due_date": job_offers.earliest_due_date().isoformat(),
            "max_deliverables_chars": job_offers.MAX_DELIVERABLES_CHARS,
            "max_note_chars": job_offers.MAX_NOTE_CHARS,
        },
        status_code=status_code,
    )


@router.get(
    "/discover/opportunity/{opportunity_id}/applicants/{application_id}/offer",
    response_class=HTMLResponse,
)
async def discover_opportunity_offer_form(
    opportunity_id: str,
    application_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Step 6A: the Make offer form, for the poster only. If an offer
    already exists, go back to the Application page (shows Offer sent)."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    listing, application = _offer_target_or_raise(
        opportunity_id, application_id, session["user_id"]
    )
    if job_offers.get_for_application(application["id"], session["user_id"]):
        return RedirectResponse(
            f"/brand/discover/opportunity/{listing['id']}/applicants/{application['id']}",
            status_code=303,
        )
    return _offer_form_response(
        request, profile, listing, application, values={}, errors={}
    )


@router.post("/discover/opportunity/{opportunity_id}/applicants/{application_id}/offer")
async def discover_opportunity_offer_submit(
    opportunity_id: str,
    application_id: str,
    request: Request,
    amount: str = Form(""),
    deliverables: str = Form(""),
    due_date: str = Form(""),
    note: str = Form(""),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Step 6A: persist the one offer, then POST/Redirect/GET. Only the four
    terms come from the form; who is involved comes from the listing /
    application rows and the session."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    listing, application = _offer_target_or_raise(
        opportunity_id, application_id, session["user_id"]
    )
    application_path = (
        f"/brand/discover/opportunity/{listing['id']}/applicants/{application['id']}"
    )
    if job_offers.get_for_application(application["id"], session["user_id"]):
        return RedirectResponse(application_path, status_code=303)
    values = {
        "amount": amount[:64],
        "deliverables": deliverables[:4000],
        "due_date": due_date[:32],
        "note": note[:4000],
    }
    terms, errors = job_offers.validate_terms(
        amount=amount, deliverables=deliverables, due_date=due_date, note=note
    )
    if terms is None:
        return _offer_form_response(
            request, profile, listing, application,
            values=values, errors=errors, status_code=400,
        )
    outcome, _ = job_offers.create(
        listing=listing,
        application=application,
        poster_user_id=session["user_id"],
        terms=terms,
    )
    if outcome == job_offers.CREATED:
        return RedirectResponse(f"{application_path}/offer/sent", status_code=303)
    if outcome == job_offers.DUPLICATE:
        return RedirectResponse(application_path, status_code=303)
    if outcome == job_offers.REFUSED:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN)
    return _offer_form_response(
        request,
        profile,
        listing,
        application,
        values=values,
        errors={},
        form_error="couldn't send that offer. try again in a moment.",
        status_code=503,
    )


@router.get(
    "/discover/opportunity/{opportunity_id}/applicants/{application_id}/offer/sent",
    response_class=HTMLResponse,
)
async def discover_opportunity_offer_sent(
    opportunity_id: str,
    application_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    """Step 6A: confirmation after a successful send. Only shown when the
    offer really exists; otherwise back to the form."""
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    listing, application = _offer_target_or_raise(
        opportunity_id, application_id, session["user_id"]
    )
    application_path = (
        f"/brand/discover/opportunity/{listing['id']}/applicants/{application['id']}"
    )
    if not job_offers.get_for_application(application["id"], session["user_id"]):
        return RedirectResponse(f"{application_path}/offer", status_code=303)
    return templates.TemplateResponse(
        request,
        "creator/opportunity_offer_sent.html",
        {
            "profile": profile,
            "listing": listing,
            "applicant": job_applications.attach_applicants([application])[0]["applicant"],
            "application_path": application_path,
            "applicants_path": f"/brand/discover/opportunity/{listing['id']}/applicants",
        },
    )


@router.get("/discover/brand/{brand_user_id}", response_class=HTMLResponse)
async def discover_brand_detail(
    brand_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    card = discover.get_card(card_kind="brand", card_id=brand_user_id)
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


def _form_strings(form: Any, field: str) -> list[str]:
    """Project a Starlette ``FormData.getlist`` result to string-only.

    ``FormData.getlist`` is typed ``list[UploadFile | str]`` because the
    same object carries file uploads on multipart forms. Chip groups
    never accept files; this helper narrows the boundary so the chip
    cleaner stays typed ``list[str]``."""
    return [v for v in form.getlist(field) if isinstance(v, str)]


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
    return discover.clean_kind(value)


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
    return [_brand_card(card) for card in cards]


def _brand_card(card: dict) -> dict:
    card = dict(card)
    card["detail_path"] = _brand_detail_path(card)
    return card


def _brand_detail_path(card: dict) -> str:
    if card["card_kind"] == "opportunity":
        return f"/brand/discover/opportunity/{card['card_id']}"
    if card["card_kind"] == "brand":
        return f"/brand/discover/brand/{card['card_id']}"
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
