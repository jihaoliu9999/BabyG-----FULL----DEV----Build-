"""Creator dashboard, intel feedback, notifications, and brand view.

The chat surface arrives in Phase 1 Step 7. Until then, "outreach" from
brands lands as a notification linking to the brand's read-only profile.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.deps import require_role
from app.services import brands, dms, intel, network, notifications, profiles

router = APIRouter(tags=["creator"])

CATEGORY_LABELS = {
    "venue": "Venue",
    "trend": "Trend",
    "brand": "Brand",
    "collab": "Collab",
    "alert": "Alert",
}


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
    unread_notifs = notifications.list_unread(session["user_id"], limit=4)
    unread_total = notifications.unread_count(session["user_id"])
    unread_dms = dms.unread_count_for_user(session["user_id"])

    return templates.TemplateResponse(
        request,
        "creator/dashboard.html",
        {
            "profile": profile,
            "posts": posts,
            "feedback_map": feedback_map,
            "categories": list(CATEGORY_LABELS.items()),
            "active_category": category if category in intel.CATEGORIES else None,
            "unread_notifs": unread_notifs,
            "unread_total": unread_total,
            "unread_dms": unread_dms,
        },
    )


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
    return RedirectResponse(target or "/creator/notifications", status_code=303)


@router.post("/creator/notifications/read-all")
async def notifications_mark_all_read(
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    notifications.mark_all_read(session["user_id"])
    return RedirectResponse("/creator/notifications", status_code=303)


# -----------------------------------------------------------------------------
# Read-only brand profile (entry point for collab_match notifications)
# -----------------------------------------------------------------------------


@router.get("/creator/brands/{brand_user_id}", response_class=HTMLResponse)
async def brand_view(
    brand_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    brand = brands.get_by_user_id(brand_user_id)
    if brand is None or not brand.get("is_verified"):
        # Unverified brands shouldn't be reachable as a profile page —
        # if a creator follows a stale link, return 404.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    existing_thread = dms.get_thread_between(session["user_id"], brand_user_id)
    return templates.TemplateResponse(
        request,
        "creator/brand_view.html",
        {"brand": brand, "existing_thread": bool(existing_thread)},
    )


# -----------------------------------------------------------------------------
# DMs
# -----------------------------------------------------------------------------


@router.get("/creator/dm", response_class=HTMLResponse)
async def dm_list(
    request: Request, session: SessionPayload = Depends(require_role("creator"))
) -> Response:
    threads = dms.list_threads_for_user(session["user_id"])
    # The creator only DMs verified brands in this step, so peer profiles
    # come from the brands service.
    peers = {t["peer_id"]: brands.get_by_user_id(t["peer_id"]) for t in threads}
    return templates.TemplateResponse(
        request,
        "creator/dm_list.html",
        {"threads": threads, "peers": peers},
    )


@router.get("/creator/dm/{peer_user_id}", response_class=HTMLResponse)
async def dm_thread(
    peer_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    peer = brands.get_by_user_id(peer_user_id)
    if peer is None or not peer.get("is_verified"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    thread = dms.get_or_create_thread(session["user_id"], peer_user_id)
    if thread is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR)
    messages = dms.list_messages(str(thread["id"]))
    dms.mark_thread_read_for(str(thread["id"]), reader_id=session["user_id"])
    return templates.TemplateResponse(
        request,
        "creator/dm_thread.html",
        {
            "thread": thread,
            "messages": messages,
            "peer": peer,
            "peer_id": peer_user_id,
            "me_id": session["user_id"],
        },
    )


@router.post("/creator/dm/{peer_user_id}/send")
async def dm_send(
    peer_user_id: str,
    body: str = Form(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    peer = brands.get_by_user_id(peer_user_id)
    if peer is None or not peer.get("is_verified"):
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
        notifications.create(
            user_id=peer_user_id,
            kind="new_dm",
            title=f"New message from {sender_label}",
            body=body[:160],
            link_path=f"/brand/dm/{session['user_id']}",
        )
    return RedirectResponse(f"/creator/dm/{peer_user_id}", status_code=303)


# -----------------------------------------------------------------------------
# Network: directory + peer profile + connections
# -----------------------------------------------------------------------------


@router.get("/creator/network", response_class=HTMLResponse)
async def network_directory(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    creators = network.list_directory_for_creator(session["user_id"])
    pending_in = len(network.list_incoming_pending(session["user_id"]))
    return templates.TemplateResponse(
        request,
        "creator/network_list.html",
        {"creators": creators, "pending_in": pending_in},
    )


@router.get("/creator/network/{peer_user_id}", response_class=HTMLResponse)
async def network_profile(
    peer_user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if peer_user_id == session["user_id"]:
        return RedirectResponse("/creator/network", status_code=302)

    peer = profiles.get_creator_profile(peer_user_id)
    if peer is None or not peer.get("onboarding_completed_at"):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    connection = network.get_connection_between(session["user_id"], peer_user_id)
    if connection is not None and connection.get("status") == "blocked":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

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
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    accepted = network.list_accepted_for_user(session["user_id"])
    incoming = network.list_incoming_pending(session["user_id"])
    outgoing = network.list_outgoing_pending(session["user_id"])

    peer_ids = {row["peer_id"] for row in accepted + incoming + outgoing}
    peers = {pid: profiles.get_creator_profile(pid) for pid in peer_ids}
    return templates.TemplateResponse(
        request,
        "creator/connections_list.html",
        {
            "accepted": accepted,
            "incoming": incoming,
            "outgoing": outgoing,
            "peers": peers,
        },
    )


@router.post("/creator/connections/{connection_id}/{action}")
async def connection_respond(
    connection_id: str,
    action: str,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    if action not in network.RESPOND_ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)
    network.respond_to_connection(
        connection_id=connection_id,
        responder_id=session["user_id"],
        action=action,
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
