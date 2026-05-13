"""Operator console — minimal in this step. Just enough to publish intel.

Polished operator UX (verification queue, flagged messages, member roster,
analytics) lands later. For now: list intel posts, create one, edit one,
archive one.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.core.security import SessionPayload
from app.core.templating import templates
from app.core.url_guard import http_url_or_none
from app.deps import require_role
from app.routes.onboarding import CREATOR_NICHES  # reuse vocabulary
from app.services import (
    abuse,
    audit,
    dms,
    intel,
    jobs,
    members,
    notifications,
    operator_notes,
    profiles,
)

router = APIRouter(prefix="/operator", tags=["operator"])

NICHES = CREATOR_NICHES
TIERS = intel.TIERS
CATEGORIES = intel.CATEGORIES
CONFIDENCES = intel.CONFIDENCES
PUBLISH_STATUSES = ["draft", "active"]                          # what the form offers
ALL_STATUSES = list(intel.STATUSES)                              # what the DB allows


@router.get("", response_class=HTMLResponse)
async def console_home(
    request: Request, session: SessionPayload = Depends(require_role("operator"))
) -> Response:
    counts = _status_counts()
    pending_abuse = abuse.count_pending()
    return templates.TemplateResponse(
        request,
        "operator/console.html",
        {
            "counts": counts,
            "pending_abuse": pending_abuse,
        },
    )


# -----------------------------------------------------------------------------
# Intel CRUD
# -----------------------------------------------------------------------------


@router.get("/intel", response_class=HTMLResponse)
async def intel_list(
    request: Request,
    status_filter: str | None = Query(None, alias="status"),
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    posts = intel.list_for_operator(status=status_filter)
    return templates.TemplateResponse(
        request,
        "operator/intel_list.html",
        {
            "posts": posts,
            "active_status": status_filter,
            "statuses": intel.STATUSES,
        },
    )


@router.get("/intel/new", response_class=HTMLResponse)
async def intel_new_form(
    request: Request, session: SessionPayload = Depends(require_role("operator"))
) -> Response:
    # `<input type="datetime-local">` strips any timezone offset, so we
    # can't pre-fill an aware ISO string ("2026-05-15T12:00+00:00") — the
    # browser silently drops the `+00:00` and the operator ends up with
    # a different time than displayed. Drop tzinfo before formatting.
    default_until = (
        (datetime.now(UTC) + timedelta(days=7))
        .replace(tzinfo=None)
        .isoformat(timespec="minutes")
    )
    return templates.TemplateResponse(
        request,
        "operator/intel_form.html",
        {
            "post": {
                "valid_until": default_until,
                "status": "draft",
                "confidence": "medium",
                "city": "Miami",
                "target_tiers": ["basic", "pro", "vip"],
            },
            "vocab": _vocab(),
            "error": None,
            "is_new": True,
            "post_id": None,
        },
    )


@router.post("/intel")
async def intel_create(
    request: Request, session: SessionPayload = Depends(require_role("operator"))
) -> Response:
    form = await request.form()
    payload, error = _validate_intel(form)
    if error:
        return _form_error(request, form, error, is_new=True, post_id=None)

    new_id = intel.create_intel_post(created_by=session["user_id"], payload=payload)
    if not new_id:
        return _form_error(
            request,
            form,
            "We couldn't save the post. Try again.",
            is_new=True,
            post_id=None,
        )
    return RedirectResponse("/operator/intel", status_code=303)


@router.get("/intel/{post_id}/edit", response_class=HTMLResponse)
async def intel_edit_form(
    post_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    post = intel.get_intel_post(post_id)
    if post is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "operator/intel_form.html",
        {
            "post": post,
            "vocab": _vocab(),
            "error": None,
            "is_new": False,
            "post_id": post_id,
        },
    )


@router.post("/intel/{post_id}")
async def intel_update(
    post_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    form = await request.form()
    payload, error = _validate_intel(form)
    if error:
        return _form_error(request, form, error, is_new=False, post_id=post_id)
    if not intel.update_intel_post(post_id, payload):
        return _form_error(
            request, form, "Couldn't update the post.", is_new=False, post_id=post_id
        )
    return RedirectResponse("/operator/intel", status_code=303)


@router.post("/intel/{post_id}/archive")
async def intel_archive(
    post_id: str, session: SessionPayload = Depends(require_role("operator"))
) -> Response:
    intel.archive_intel_post(post_id)
    return RedirectResponse("/operator/intel", status_code=303)


# -----------------------------------------------------------------------------
# Validation
# -----------------------------------------------------------------------------


def _validate_intel(form) -> tuple[dict[str, Any], str | None]:
    title = _str(form.get("title"), 140)
    body = _str(form.get("body"), 4000)
    category = _enum(form.get("category"), CATEGORIES)
    confidence = _enum(form.get("confidence"), CONFIDENCES) or "medium"
    valid_until = _str(form.get("valid_until"), 64)
    valid_from = _str(form.get("valid_from"), 64)
    source = _str(form.get("source"), 500)
    city = _str(form.get("city"), 60) or "Miami"
    # Accept any DB-valid status from the form so editing a `scheduled`
    # or `expired` post doesn't silently demote it. The form only
    # *renders* draft/active radios, but a rendered hidden input or a
    # round-trip from get_intel_post may carry the original.
    publish_status = _enum(form.get("status"), ALL_STATUSES) or "draft"
    target_niches = _multi(form.getlist("target_niches"), NICHES)
    target_tiers = _multi(form.getlist("target_tiers"), TIERS) or list(TIERS)

    if not title:
        return {}, "Please enter a title."
    if not body:
        return {}, "Please enter a body."
    if not category:
        return {}, "Pick a category."
    if not valid_until:
        return {}, "Please set a valid_until date."
    if source:
        safe_source = http_url_or_none(source)
        if safe_source is None:
            return {}, "Source must be a valid http(s) URL."
        source = safe_source

    payload: dict[str, Any] = {
        "title": title,
        "body": body,
        "category": category,
        "confidence": confidence,
        "valid_until": valid_until,
        "source": source or None,
        "city": city,
        "status": publish_status,
        "target_niches": target_niches,        # may be empty (= all niches)
        "target_tiers": target_tiers,
    }
    if valid_from:
        payload["valid_from"] = valid_from

    return payload, None


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _form_error(
    request: Request, form, message: str, *, is_new: bool, post_id: str | None
) -> Response:
    return templates.TemplateResponse(
        request,
        "operator/intel_form.html",
        {
            "post": _form_to_post(form),
            "vocab": _vocab(),
            "error": message,
            "is_new": is_new,
            "post_id": post_id,
        },
        status_code=400,
    )


def _form_to_post(form) -> dict[str, Any]:
    multi_keys = {"target_niches", "target_tiers"}
    out: dict[str, Any] = {}
    for key in form:
        out[key] = list(form.getlist(key)) if key in multi_keys else form.get(key)
    return out


def _vocab() -> dict[str, list[str]]:
    return {
        "categories": CATEGORIES,
        "confidences": CONFIDENCES,
        "niches": NICHES,
        "tiers": TIERS,
        "statuses": PUBLISH_STATUSES,
    }


def _status_counts() -> dict[str, int]:
    return intel.status_counts()


def _str(value: Any, max_len: int) -> str:
    if value is None:
        return ""
    return str(value).strip()[:max_len]


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


# -----------------------------------------------------------------------------
# Abuse / moderation queue
# -----------------------------------------------------------------------------


@router.get("/abuse", response_class=HTMLResponse)
async def abuse_list(
    request: Request,
    tab: str = Query("pending"),
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    if tab not in abuse.STATUSES:
        tab = "pending"
    rows = abuse.list_by_status(tab)
    return templates.TemplateResponse(
        request,
        "operator/abuse_list.html",
        {"reports": rows, "active_tab": tab, "tabs": abuse.STATUSES},
    )


@router.get("/abuse/{report_id}", response_class=HTMLResponse)
async def abuse_detail(
    report_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    report = abuse.get(report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    context = _abuse_target_context(report)
    return templates.TemplateResponse(
        request,
        "operator/abuse_detail.html",
        {"report": report, "context": context, "error": None},
    )


@router.post("/abuse/{report_id}/{action}")
async def abuse_resolve(
    report_id: str,
    action: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    if action not in abuse.RESOLUTION_ACTIONS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST)

    form = await request.form()
    notes = _str(form.get("notes"), 1000)

    report = abuse.get(report_id)
    if report is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    if not abuse.resolve(
        report_id=report_id,
        reviewer_id=session["user_id"],
        action=action,
        notes=notes or None,
    ):
        return templates.TemplateResponse(
            request,
            "operator/abuse_detail.html",
            {
                "report": report,
                "context": _abuse_target_context(report),
                "error": (
                    "Notes are required for action and escalate."
                    if action != "dismiss"
                    else "Couldn't update the report. Try again."
                ),
            },
            status_code=400,
        )

    notifications.create(
        user_id=report["reporter_id"],
        kind="flag_update",
        title=_resolve_title(action),
        body=notes or None,
        link_path="/creator/notifications",
    )
    audit.record(
        actor_user_id=session["user_id"],
        action=f"abuse.{action}",
        target_type="abuse_report",
        target_id=report_id,
        notes=notes or None,
    )
    return RedirectResponse("/operator/abuse", status_code=303)


# -----------------------------------------------------------------------------
# Helpers for the abuse queue
# -----------------------------------------------------------------------------


def _abuse_target_context(report: dict[str, Any]) -> dict[str, Any]:
    """Resolve a small preview of the reported target so the operator
    has enough to decide without leaving the page. None of these calls
    raise — if a target is missing we just render an "unavailable" hint.
    """
    target_type = report.get("target_type")
    target_id = report.get("target_id")
    out: dict[str, Any] = {"kind": target_type, "available": False}

    if not target_id:
        return out

    if target_type == "dm_thread":
        messages = dms.list_messages_for_operator(str(target_id), limit=20)
        out["available"] = bool(messages)
        out["messages"] = messages
        return out

    if target_type == "message":
        # No direct lookup-by-message-id helper yet; we just mark it
        # unavailable. Operators can navigate to the thread via the
        # report's reason text in practice. Add a service helper if
        # this becomes common.
        out["available"] = False
        return out

    if target_type == "profile":
        # The reported user_id is in target_id. v1 is creator-only, so
        # we look up creator_profiles; brand profile lookups return
        # nothing now that the surface is removed.
        creator = profiles.get_creator_profile(str(target_id))
        if creator is not None:
            out["available"] = True
            out["profile_kind"] = "creator"
            out["profile"] = creator
        return out

    # listing — deferred until the listings surface ships
    return out


def _resolve_title(action: str) -> str:
    return {
        "dismiss": "Your report was reviewed.",
        "action": "Action taken on your report.",
        "escalate": "Your report has been escalated.",
    }[action]


# -----------------------------------------------------------------------------
# Job listings (operator: list + take down)
# -----------------------------------------------------------------------------


@router.get("/jobs", response_class=HTMLResponse)
async def operator_jobs_list(
    request: Request,
    tab: str = Query("active"),
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    if tab == "taken_down":
        listings = jobs.list_for_operator(taken_down=True)
    else:
        tab = "active"
        listings = jobs.list_for_operator(taken_down=False)
    poster_ids = sorted({str(lst["poster_user_id"]) for lst in listings})
    poster_profiles = profiles.get_creators_by_ids(poster_ids)
    return templates.TemplateResponse(
        request,
        "operator/jobs_list.html",
        {
            "listings": listings,
            "poster_profiles": poster_profiles,
            "active_tab": tab,
        },
    )


@router.get("/jobs/{listing_id}", response_class=HTMLResponse)
async def operator_jobs_detail(
    listing_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    listing = jobs.get(listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    poster = profiles.get_creator_profile(str(listing["poster_user_id"]))
    return templates.TemplateResponse(
        request,
        "operator/jobs_detail.html",
        {"listing": listing, "poster": poster, "error": None},
    )


@router.post("/jobs/{listing_id}/takedown")
async def operator_jobs_takedown(
    listing_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    form = await request.form()
    reason = _str(form.get("reason"), 1000)
    if not reason:
        listing = jobs.get(listing_id)
        return templates.TemplateResponse(
            request,
            "operator/jobs_detail.html",
            {
                "listing": listing or {"id": listing_id},
                "poster": (
                    profiles.get_creator_profile(str(listing["poster_user_id"]))
                    if listing else None
                ),
                "error": "Reason is required for takedown.",
            },
            status_code=400,
        )
    if not jobs.take_down(
        listing_id=listing_id, operator_id=session["user_id"], reason=reason
    ):
        listing = jobs.get(listing_id)
        return templates.TemplateResponse(
            request,
            "operator/jobs_detail.html",
            {
                "listing": listing or {"id": listing_id},
                "poster": None,
                "error": "Couldn't take down the listing. Try again.",
            },
            status_code=400,
        )
    listing = jobs.get(listing_id)
    if listing:
        notifications.create(
            user_id=str(listing["poster_user_id"]),
            kind="flag_update",
            title="A listing of yours was taken down.",
            body=reason,
            link_path="/creator/jobs/mine",
        )
    audit.record(
        actor_user_id=session["user_id"],
        action="listing.takedown",
        target_type="listing",
        target_id=listing_id,
        notes=reason,
    )
    return RedirectResponse("/operator/jobs", status_code=303)


# -----------------------------------------------------------------------------
# Member roster + per-user notes
# -----------------------------------------------------------------------------


@router.get("/members", response_class=HTMLResponse)
async def members_list(
    request: Request,
    role: str | None = Query(None),
    page: int = Query(1, ge=1),
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    rows, total = members.list_users(role=role, page=page, page_size=100)
    return templates.TemplateResponse(
        request,
        "operator/members_list.html",
        {
            "members": rows,
            "active_role": role,
            "page": page,
            "total": total,
            "has_next": (page * 100) < total,
            "has_prev": page > 1,
        },
    )


@router.get("/members/{user_id}", response_class=HTMLResponse)
async def member_detail(
    user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    user = members.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    members.annotate_with_profile(user)
    notes = operator_notes.list_for_user(user_id)
    return templates.TemplateResponse(
        request,
        "operator/member_detail.html",
        {"member": user, "notes": notes, "error": None},
    )


@router.post("/members/{user_id}/note")
async def member_note_create(
    user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    form = await request.form()
    body = _str(form.get("body"), 2000)
    if not body:
        user = members.get_user(user_id)
        if user is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
        members.annotate_with_profile(user)
        return templates.TemplateResponse(
            request,
            "operator/member_detail.html",
            {
                "member": user,
                "notes": operator_notes.list_for_user(user_id),
                "error": "Notes can't be empty.",
            },
            status_code=400,
        )
    operator_notes.create(
        target_user_id=user_id,
        author_user_id=session["user_id"],
        body=body,
    )
    audit.record(
        actor_user_id=session["user_id"],
        action="operator_note.create",
        target_type="user",
        target_id=user_id,
    )
    return RedirectResponse(f"/operator/members/{user_id}", status_code=303)


# -----------------------------------------------------------------------------
# Audit log
# -----------------------------------------------------------------------------


@router.get("/audit", response_class=HTMLResponse)
async def audit_list(
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    rows = audit.list_recent()
    return templates.TemplateResponse(
        request, "operator/audit_list.html", {"rows": rows}
    )
