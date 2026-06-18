"""Mobile-first unified Discover routes."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import discover, network, notifications, profiles

router = APIRouter(prefix="/creator/discover", tags=["creator", "discover"])


@router.get("", response_class=HTMLResponse)
async def discover_page(
    request: Request,
    kind: str = Query("all"),
    category: str | None = Query(None),
    location: str | None = Query(None),
    budget_min: int | None = Query(None, ge=0),
    budget_max: int | None = Query(None, ge=0),
    bring_back_kind: str | None = Query(None),
    bring_back_id: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    profile = profiles.get_creator_profile(session["user_id"]) or {}
    kind_clean = discover.clean_kind(kind)
    prioritize = None
    if bring_back_kind and bring_back_id:
        prioritize = (discover.clean_kind(bring_back_kind), bring_back_id)
    cards = discover.list_cards(
        viewer_id=session["user_id"],
        viewer_role="creator",
        kind=kind_clean,
        category=category,
        location=location,
        budget_min=budget_min,
        budget_max=budget_max,
        viewer_tags=list(profile.get("niches") or []),
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
            "category": category or "",
            "location": location or "",
            "budget_min": budget_min,
            "budget_max": budget_max,
            "can_undo": discover.last_undoable_pass(session["user_id"]) is not None,
            "discover_base_path": "/creator/discover",
            "discover_swipe_path": "/creator/discover/swipe",
            "discover_undo_path": "/creator/discover/undo",
        },
    )


@router.get("/brand/{brand_user_id}", response_class=HTMLResponse)
async def discover_brand_detail(
    brand_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    card = discover.get_card(card_kind="brand", card_id=brand_user_id)
    if card is None or card["owner_user_id"] == session["user_id"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "creator/discover_brand.html",
        {"card": card},
    )


@router.post("/swipe")
async def discover_swipe(
    target_kind: str = Form(...),
    target_card_id: str = Form(...),
    action: str = Form(...),
    kind: str = Form("all"),
    category: str = Form(""),
    location: str = Form(""),
    budget_min: int | None = Form(None, ge=0),
    budget_max: int | None = Form(None, ge=0),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    action_clean = str(action or "").strip().lower()
    if action_clean not in {"passed", "saved", "connected", "interested", "opened_profile"}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    card = discover.get_card(card_kind=target_kind, card_id=target_card_id)
    if card is None or card["owner_user_id"] == session["user_id"]:
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
        return RedirectResponse(str(card.get("detail_path") or "/creator/discover"), 303)
    return RedirectResponse(
        _discover_url(kind, category, location, budget_min, budget_max), 303
    )


@router.post("/undo")
async def discover_undo(
    kind: str = Form("all"),
    category: str = Form(""),
    location: str = Form(""),
    budget_min: int | None = Form(None, ge=0),
    budget_max: int | None = Form(None, ge=0),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    previous = discover.last_undoable_pass(session["user_id"])
    if previous is None:
        return RedirectResponse(
            _discover_url(kind, category, location, budget_min, budget_max), 303
        )
    target_kind, target_card_id = previous
    card = discover.get_card(card_kind=target_kind, card_id=target_card_id)
    discover.record_action(
        user_id=session["user_id"],
        target_kind=target_kind,
        target_card_id=target_card_id,
        target_user_id=card["owner_user_id"] if card else None,
        action_type="undo_pass",
    )
    params = {
        "kind": discover.clean_kind(kind),
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
    return RedirectResponse(f"/creator/discover?{urlencode(params)}", 303)


def _discover_url(
    kind: str,
    category: str,
    location: str,
    budget_min: int | None = None,
    budget_max: int | None = None,
) -> str:
    params = {"kind": discover.clean_kind(kind)}
    if category:
        params["category"] = category
    if location:
        params["location"] = location
    if budget_min is not None:
        params["budget_min"] = str(budget_min)
    if budget_max is not None:
        params["budget_max"] = str(budget_max)
    return f"/creator/discover?{urlencode(params)}"
