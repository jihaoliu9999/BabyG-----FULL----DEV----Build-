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
from app.deps import require_role
from app.routes.onboarding import CREATOR_NICHES  # reuse vocabulary
from app.services import brands, intel, notifications

router = APIRouter(prefix="/operator", tags=["operator"])

NICHES = CREATOR_NICHES
TIERS = intel.TIERS
CATEGORIES = intel.CATEGORIES
CONFIDENCES = intel.CONFIDENCES
PUBLISH_STATUSES = ["draft", "active"]                          # what an operator picks


@router.get("", response_class=HTMLResponse)
async def console_home(
    request: Request, session: SessionPayload = Depends(require_role("operator"))
) -> Response:
    counts = _status_counts()
    pending_brands = len(brands.list_pending())
    return templates.TemplateResponse(
        request,
        "operator/console.html",
        {"counts": counts, "pending_brands": pending_brands},
    )


# -----------------------------------------------------------------------------
# Brand verification queue
# -----------------------------------------------------------------------------


@router.get("/brands", response_class=HTMLResponse)
async def brands_list(
    request: Request,
    tab: str = Query("pending"),
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    if tab == "verified":
        rows = brands.list_verified()
    elif tab == "rejected":
        rows = brands.list_rejected()
    else:
        tab = "pending"
        rows = brands.list_pending()
    return templates.TemplateResponse(
        request,
        "operator/brand_list.html",
        {"brands": rows, "active_tab": tab},
    )


@router.get("/brands/{user_id}", response_class=HTMLResponse)
async def brand_detail(
    user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    brand = brands.get_by_user_id(user_id)
    if brand is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    return templates.TemplateResponse(
        request,
        "operator/brand_detail.html",
        {"brand": brand, "error": None},
    )


@router.post("/brands/{user_id}/verify")
async def brand_verify(
    user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    form = await request.form()
    notes = _str(form.get("notes"), 1000) or None
    if not brands.verify(user_id, notes):
        return _brand_detail_error(
            request, user_id, "Couldn't verify the brand. Try again."
        )
    notifications.create(
        user_id=user_id,
        kind="system",
        title="You're verified.",
        body="Your brand has been verified by babyg. You can now reach creators.",
        link_path="/brand",
    )
    return RedirectResponse("/operator/brands", status_code=303)


@router.post("/brands/{user_id}/reject")
async def brand_reject(
    user_id: str,
    request: Request,
    session: SessionPayload = Depends(require_role("operator")),
) -> Response:
    form = await request.form()
    notes = _str(form.get("notes"), 1000)
    if not notes:
        return _brand_detail_error(
            request, user_id, "Reason notes are required for rejection."
        )
    if not brands.reject(user_id, notes):
        return _brand_detail_error(
            request, user_id, "Couldn't reject the brand. Try again."
        )
    notifications.create(
        user_id=user_id,
        kind="system",
        title="Verification needs another look.",
        body=notes,
        link_path="/brand",
    )
    return RedirectResponse("/operator/brands", status_code=303)


def _brand_detail_error(request: Request, user_id: str, message: str) -> Response:
    brand = brands.get_by_user_id(user_id)
    return templates.TemplateResponse(
        request,
        "operator/brand_detail.html",
        {"brand": brand or {"user_id": user_id}, "error": message},
        status_code=400,
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
    default_until = (datetime.now(UTC) + timedelta(days=7)).isoformat(
        timespec="minutes"
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
    publish_status = _enum(form.get("status"), PUBLISH_STATUSES) or "draft"
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
    counts = {s: 0 for s in intel.STATUSES}
    for row in intel.list_for_operator(limit=500):
        s = row.get("status")
        if s in counts:
            counts[s] += 1
    return counts


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
