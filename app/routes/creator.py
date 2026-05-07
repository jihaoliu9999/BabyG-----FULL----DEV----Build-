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
from app.services import (
    bookings,
    brands,
    dms,
    intel,
    jobs,
    network,
    notifications,
    performance,
    profiles,
    receipts,
    views,
)

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
    # Peer can be a verified brand OR a connected creator. Try brand first,
    # fall back to creator profile.
    peers: dict[str, dict | None] = {}
    peer_kinds: dict[str, str] = {}
    for t in threads:
        pid = t["peer_id"]
        b = brands.get_by_user_id(pid)
        if b is not None:
            peers[pid] = b
            peer_kinds[pid] = "brand"
            continue
        c = profiles.get_creator_profile(pid)
        peers[pid] = c
        peer_kinds[pid] = "creator" if c else "unknown"
    return templates.TemplateResponse(
        request,
        "creator/dm_list.html",
        {"threads": threads, "peers": peers, "peer_kinds": peer_kinds},
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
            "peer_kind": peer_kind,
            "me_id": session["user_id"],
        },
    )


@router.post("/creator/dm/{peer_user_id}/send")
async def dm_send(
    peer_user_id: str,
    body: str = Form(...),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    peer, peer_kind = _resolve_creator_dm_peer(
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
        # Notification target depends on peer's role: brand reads at
        # /brand/dm/{me}, creator reads at /creator/dm/{me}.
        target = (
            f"/brand/dm/{session['user_id']}" if peer_kind == "brand"
            else f"/creator/dm/{session['user_id']}"
        )
        notifications.create(
            user_id=peer_user_id,
            kind="new_dm",
            title=f"New message from {sender_label}",
            body=body[:160],
            link_path=target,
        )
    return RedirectResponse(f"/creator/dm/{peer_user_id}", status_code=303)


def _resolve_creator_dm_peer(
    *, me_id: str, peer_user_id: str
) -> tuple[dict | None, str]:
    """Look up the DM peer for a creator-side route.

    A creator may DM:
      * any verified brand (always)
      * another creator only if there's an `accepted` connection between
        them (creator-creator DMs are gated to deter cold messaging)

    Returns (peer_profile, peer_kind) or (None, "") if the peer isn't
    reachable from this creator. peer_kind is "brand" or "creator".
    """
    brand = brands.get_by_user_id(peer_user_id)
    if brand is not None and brand.get("is_verified"):
        return brand, "brand"

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

    # Record the view (best-effort; don't block render).
    views.record_view(viewer_id=session["user_id"], viewed_id=peer_user_id)

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
# Job listings (creator-side)
# -----------------------------------------------------------------------------


JOB_TYPES = list(jobs.LISTING_TYPES)


@router.get("/creator/jobs", response_class=HTMLResponse)
async def jobs_board(
    request: Request,
    niche: str | None = Query(None),
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    listings = jobs.list_active(niche=niche)
    poster_ids = {str(lst["poster_user_id"]) for lst in listings}
    poster_profiles = {pid: profiles.get_creator_profile(pid) for pid in poster_ids}
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
            request, form, "Couldn't save the listing. Try again.",
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
    poster = profiles.get_creator_profile(str(listing["poster_user_id"]))
    is_mine = str(listing["poster_user_id"]) == session["user_id"]

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
    if not jobs.update(listing_id, payload):
        return _jobs_form_error(
            request, form, "Couldn't save the listing. Try again.",
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


# -----------------------------------------------------------------------------
# Job listings: validation + helpers
# -----------------------------------------------------------------------------


def _validate_listing(form):
    title = (form.get("title") or "").strip()[:140]
    description = (form.get("description") or "").strip()[:4000]
    listing_type = (form.get("listing_type") or "").strip()
    compensation = (form.get("compensation_text") or "").strip()[:240]
    deadline = (form.get("deadline") or "").strip()[:64]
    is_active = (form.get("is_active") or "").strip() != "off"

    if not title:
        return {}, "Please enter a title."
    if not description:
        return {}, "Please enter a description."
    if listing_type not in jobs.LISTING_TYPES:
        return {}, "Pick a listing type."

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
        "compensation_text": compensation or None,
        "target_niches": target_niches,
        "deadline": deadline or None,
        "is_active": is_active,
    }
    return payload, None


def _jobs_form_error(request, form, message, *, is_new, listing_id):
    listing = {
        "title": form.get("title", ""),
        "description": form.get("description", ""),
        "listing_type": form.get("listing_type", "collab"),
        "compensation_text": form.get("compensation_text", ""),
        "target_niches": list(form.getlist("target_niches")),
        "deadline": form.get("deadline", ""),
        "is_active": (form.get("is_active") or "").strip() != "off",
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
    return templates.TemplateResponse(
        request,
        "creator/calendar_list.html",
        {"bookings": rows, "horizon": horizon},
    )


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
# Weekly performance logs
# -----------------------------------------------------------------------------


@router.get("/creator/performance", response_class=HTMLResponse)
async def performance_list(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    rows = performance.list_for_user(session["user_id"])
    return templates.TemplateResponse(
        request, "creator/performance_list.html", {"logs": rows}
    )


@router.get("/creator/performance/new", response_class=HTMLResponse)
async def performance_new_form(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    return templates.TemplateResponse(
        request,
        "creator/performance_form.html",
        {
            "log": {"week_start_date": performance.last_monday_iso()},
            "error": None,
        },
    )


@router.post("/creator/performance")
async def performance_save(
    request: Request,
    session: SessionPayload = Depends(require_role("creator")),
) -> Response:
    form = await request.form()
    payload, error = _validate_performance(form)
    if error:
        return templates.TemplateResponse(
            request,
            "creator/performance_form.html",
            {
                "log": {
                    "week_start_date": form.get("week_start_date", ""),
                    "engagement_rate": form.get("engagement_rate", ""),
                    "follower_delta": form.get("follower_delta", ""),
                    "active_brand_deals_count": form.get("active_brand_deals_count", ""),
                    "active_brand_deals_value": form.get("active_brand_deals_value", ""),
                    "notes": form.get("notes", ""),
                },
                "error": error,
            },
            status_code=400,
        )
    if not performance.upsert(
        user_id=session["user_id"], entered_by=session["user_id"], payload=payload
    ):
        return templates.TemplateResponse(
            request,
            "creator/performance_form.html",
            {"log": payload, "error": "Couldn't save. Try again."},
            status_code=400,
        )
    return RedirectResponse("/creator/performance", status_code=303)


# -----------------------------------------------------------------------------
# Validation helpers for receipts + performance
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


def _validate_performance(form):
    week_start = (form.get("week_start_date") or "").strip()[:10]
    if not week_start:
        return {}, "Pick the week start date (the Monday)."
    eng_raw = (form.get("engagement_rate") or "").strip()
    delta_raw = (form.get("follower_delta") or "").strip()
    deals_count_raw = (form.get("active_brand_deals_count") or "").strip()
    deals_value_raw = (form.get("active_brand_deals_value") or "").strip()
    notes = (form.get("notes") or "").strip()[:2000]

    eng = _maybe_float(eng_raw)
    if eng_raw and eng is None:
        return {}, "Engagement rate must be a number (e.g. 4.2)."
    delta = _maybe_int(delta_raw)
    if delta_raw and delta is None:
        return {}, "Follower delta must be a whole number (positive or negative)."
    deals_count = _maybe_int(deals_count_raw) or 0
    deals_value = _maybe_float(deals_value_raw) or 0

    payload = {
        "week_start_date": week_start,
        "engagement_rate": eng,
        "follower_delta": delta,
        "active_brand_deals_count": deals_count,
        "active_brand_deals_value": deals_value,
        "notes": notes or None,
    }
    return payload, None


def _maybe_int(s: str):
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        return None


def _maybe_float(s: str):
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None
