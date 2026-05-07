"""Brand console — /brand, creator discovery, outreach, DMs.

Outreach now creates a DM thread + the first message + a notification
linking to the thread. Subsequent sends use the same thread.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import brands, creators, dms, notifications

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/brand", tags=["brand"])


# -----------------------------------------------------------------------------
# Console + discovery
# -----------------------------------------------------------------------------


@router.get("", response_class=HTMLResponse)
async def console(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    profile = brands.get_by_user_id(session["user_id"])
    if profile is None or not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)

    if not profile.get("is_verified"):
        return templates.TemplateResponse(
            request, "brand/console_pending.html", {"profile": profile}
        )

    matches = creators.list_for_brand_match(
        niche_preferences=profile.get("niche_preferences") or [],
        creator_size_preferences=profile.get("creator_size_preferences") or [],
    )
    unread_dms = dms.unread_count_for_user(session["user_id"])
    return templates.TemplateResponse(
        request,
        "brand/console.html",
        {"profile": profile, "matches": matches[:24], "unread_dms": unread_dms},
    )


@router.get("/creators", response_class=HTMLResponse)
async def creators_list(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile
    matches = creators.list_for_brand_match(
        niche_preferences=profile.get("niche_preferences") or [],
        creator_size_preferences=profile.get("creator_size_preferences") or [],
    )
    return templates.TemplateResponse(
        request,
        "brand/creators_list.html",
        {"profile": profile, "matches": matches},
    )


@router.get("/creators/{creator_user_id}", response_class=HTMLResponse)
async def creator_detail(
    creator_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile

    creator = creators.get_for_view(creator_user_id)
    if creator is None or not creator.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    existing_thread = dms.get_thread_between(session["user_id"], creator_user_id)
    return templates.TemplateResponse(
        request,
        "brand/creator_detail.html",
        {
            "profile": profile,
            "creator": creator,
            "error": None,
            "pitch": "",
            "existing_thread": bool(existing_thread),
        },
    )


@router.post("/creators/{creator_user_id}/outreach")
async def creator_outreach(
    creator_user_id: str,
    request: Request,
    pitch: str = Form(...),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile

    pitch = (pitch or "").strip()[:500]
    if len(pitch) < 20:
        return _outreach_error(
            request,
            creator_user_id=creator_user_id,
            profile=profile,
            pitch=pitch,
            message="Please write at least a couple of sentences (20+ characters).",
        )

    creator = creators.get_for_view(creator_user_id)
    if creator is None or not creator.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    thread = dms.get_or_create_thread(session["user_id"], creator_user_id)
    if thread is None:
        return _outreach_error(
            request, creator_user_id=creator_user_id, profile=profile,
            pitch=pitch, message="Couldn't open a thread. Try again.",
        )
    msg = dms.send_message(
        thread_id=str(thread["id"]),
        sender_id=session["user_id"],
        body=pitch,
    )
    if msg is None:
        return _outreach_error(
            request, creator_user_id=creator_user_id, profile=profile,
            pitch=pitch, message="Couldn't send. Try again.",
        )

    notifications.create(
        user_id=creator_user_id,
        kind="collab_match",
        title=f"{profile.get('company_name') or 'A brand'} wants to work with you.",
        body=pitch,
        link_path=f"/creator/dm/{session['user_id']}",
    )
    return RedirectResponse(
        f"/brand/dm/{creator_user_id}", status_code=303
    )


# -----------------------------------------------------------------------------
# DMs
# -----------------------------------------------------------------------------


@router.get("/dm", response_class=HTMLResponse)
async def dm_list(
    request: Request, session: SessionPayload = Depends(require_role("brand"))
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile
    threads = dms.list_threads_for_user(session["user_id"])
    peers = {
        t["peer_id"]: creators.get_for_view(t["peer_id"]) for t in threads
    }
    return templates.TemplateResponse(
        request,
        "brand/dm_list.html",
        {"profile": profile, "threads": threads, "peers": peers},
    )


@router.get("/dm/{peer_user_id}", response_class=HTMLResponse)
async def dm_thread(
    peer_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile

    peer = creators.get_for_view(peer_user_id)
    if peer is None or not peer.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    messages = dms.list_messages(str(thread["id"]))
    dms.mark_thread_read_for(str(thread["id"]), reader_id=session["user_id"])
    return templates.TemplateResponse(
        request,
        "brand/dm_thread.html",
        {
            "profile": profile,
            "thread": thread,
            "messages": messages,
            "peer": peer,
            "peer_id": peer_user_id,
            "me_id": session["user_id"],
            "error": None,
            "pending_body": "",
        },
    )


@router.post("/dm/{peer_user_id}/send")
async def dm_send(
    peer_user_id: str,
    request: Request,
    body: str = Form(...),
    session: SessionPayload = Depends(require_role("brand")),
) -> Response:
    profile = _verified_brand_or_redirect(session["user_id"])
    if isinstance(profile, RedirectResponse):
        return profile

    peer = creators.get_for_view(peer_user_id)
    if peer is None or not peer.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    body = (body or "").strip()
    if not body:
        return RedirectResponse(f"/brand/dm/{peer_user_id}", status_code=303)

    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    msg = dms.send_message(
        thread_id=str(thread["id"]),
        sender_id=session["user_id"],
        body=body,
    )
    if msg is not None:
        notifications.create(
            user_id=peer_user_id,
            kind="new_dm",
            title=f"New message from {profile.get('company_name') or 'a brand'}",
            body=body[:160],
            link_path=f"/creator/dm/{session['user_id']}",
        )
    return RedirectResponse(f"/brand/dm/{peer_user_id}", status_code=303)


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _verified_brand_or_redirect(user_id: str) -> dict[str, Any] | RedirectResponse:
    profile = brands.get_by_user_id(user_id)
    if profile is None or not profile.get("onboarding_completed_at"):
        return RedirectResponse("/onboarding/brand", status_code=302)
    if not profile.get("is_verified"):
        return RedirectResponse("/brand", status_code=302)
    return profile


def _outreach_error(
    request: Request,
    *,
    creator_user_id: str,
    profile: dict[str, Any],
    pitch: str,
    message: str,
) -> Response:
    creator = creators.get_for_view(creator_user_id)
    existing_thread = dms.get_thread_between(profile["user_id"], creator_user_id)
    return templates.TemplateResponse(
        request,
        "brand/creator_detail.html",
        {
            "profile": profile,
            "creator": creator or {"user_id": creator_user_id},
            "error": message,
            "pitch": pitch,
            "existing_thread": bool(existing_thread),
        },
        status_code=400,
    )
