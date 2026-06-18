"""Brand entry routes."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import discover, network, notifications, profiles

router = APIRouter(prefix="/brand", tags=["brand"])


@router.get("", response_class=HTMLResponse)
async def dashboard(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    profile = profiles.get_brand_profile(session["user_id"]) or {}
    if not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    return RedirectResponse("/brand/discover", status_code=302)


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
