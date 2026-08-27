"""babyg deal tracking. Phase 5 of the babyg AI v2 plan.

One deal per brand relationship per creator. Every DM, email, calendar
event, contract PDF, or action proposal that touched the deal links to
it through a touchpoint row.

Stage transitions live here, not in the model. The bot may hint at
where a deal is going ("we accepted"), but this module decides whether
that transition is even allowed. Bad transitions are refused, not
silently applied, so a hallucinated stage jump never corrupts memory.

Money is stored as int cents; every helper accepts a dollar figure and
converts, so callers do not do the arithmetic.

Every helper is best-effort. Supabase failures never raise into the
turn; they log and return None / [].
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Literal

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid

logger = logging.getLogger(__name__)

DealStage = Literal[
    "inquiry",
    "negotiating",
    "waiting_on_terms",
    "accepted",
    "delivered",
    "payment_pending",
    "paid",
    "stale_or_ghosted",
    "declined",
    "cancelled",
]

TouchpointKind = Literal[
    "dm_message",
    "email_message",
    "calendar_event",
    "contract_pdf",
    "action_proposal",
    "note",
]

TouchpointDirection = Literal["inbound", "outbound", "internal"]

STAGES: frozenset[str] = frozenset(
    {
        "inquiry",
        "negotiating",
        "waiting_on_terms",
        "accepted",
        "delivered",
        "payment_pending",
        "paid",
        "stale_or_ghosted",
        "declined",
        "cancelled",
    }
)

# Terminal stages: nothing about the deal changes after these are set.
# A "declined" deal never gets re-nudged (Phase 5 requirement).
TERMINAL_STAGES: frozenset[str] = frozenset({"paid", "declined", "cancelled"})

# The allowed stage graph. babyg cannot jump inquiry -> paid, or bring a
# cancelled deal back to life. Reopening a mistakenly closed deal is a
# manual operator action (Phase 3 audit path), not a bot capability.
_STAGE_GRAPH: dict[str, frozenset[str]] = {
    "inquiry": frozenset(
        {"negotiating", "declined", "cancelled", "stale_or_ghosted"}
    ),
    "negotiating": frozenset(
        {
            "waiting_on_terms",
            "accepted",
            "declined",
            "cancelled",
            "stale_or_ghosted",
        }
    ),
    "waiting_on_terms": frozenset(
        {
            "negotiating",
            "accepted",
            "declined",
            "cancelled",
            "stale_or_ghosted",
        }
    ),
    "accepted": frozenset(
        {"delivered", "cancelled", "stale_or_ghosted"}
    ),
    "delivered": frozenset(
        {"payment_pending", "paid", "cancelled"}
    ),
    "payment_pending": frozenset({"paid", "stale_or_ghosted"}),
    # Ghosted deals can come back if the brand replies.
    "stale_or_ghosted": frozenset(
        {"negotiating", "accepted", "declined", "cancelled"}
    ),
    "paid": frozenset(),
    "declined": frozenset(),
    "cancelled": frozenset(),
}

# Same brand touching us again within this window links to the existing
# deal instead of opening a new one. Phase 5 test: two DMs from the
# same brand within 24 hours link to the same deal.
_SAME_DEAL_WINDOW_HOURS = 24


def is_terminal(stage: str) -> bool:
    return stage in TERMINAL_STAGES


def can_transition(from_stage: str, to_stage: str) -> bool:
    """True iff the stage graph allows `from_stage` -> `to_stage`. A
    no-op transition (same stage) is allowed so callers can idempotently
    write the current stage without special-casing."""
    if from_stage == to_stage:
        return True
    allowed = _STAGE_GRAPH.get(from_stage)
    if allowed is None:
        return False
    return to_stage in allowed


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _dollars_to_cents(dollars: float | int | None) -> int | None:
    if dollars is None:
        return None
    try:
        return int(round(float(dollars) * 100))
    except (TypeError, ValueError):
        return None


def _service():
    return supabase_client.get_service_client()


def _normalize_brand(brand_name: str | None) -> str:
    """Case-insensitive, whitespace-collapsed brand slug for matching.
    Phase 6 (babyg_relations) will layer real identity resolution over
    this; for Phase 5 this is enough to keep two "vans" DMs on the same
    deal even if one had a trailing space."""
    return " ".join((brand_name or "").strip().lower().split())


# ---- create / find --------------------------------------------------------


def find_open_deal(
    creator_id: str,
    *,
    brand_name: str,
) -> dict[str, Any] | None:
    """Return the newest non-terminal deal for the creator against
    `brand_name`, or None if none exists. Case-insensitive brand match.
    A terminal deal (paid, declined, cancelled) is never returned by
    this helper — that is what forces a new deal row when a brand comes
    back after being declined."""
    creator_uid = safe_uuid(creator_id)
    brand_key = _normalize_brand(brand_name)
    if not creator_uid or not brand_key:
        return None
    try:
        rows = list(
            _service()
            .table("babyg_memory_deals")
            .select("*")
            .eq("creator_id", creator_uid)
            .order("last_touch_at", desc=True)
            .limit(50)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.info("babyg_deals.find_open_deal_failed", exc_info=True)
        return None
    for row in rows:
        if _normalize_brand(row.get("brand_name")) != brand_key:
            continue
        if str(row.get("stage") or "") in TERMINAL_STAGES:
            continue
        return row
    return None


def create_deal(
    creator_id: str,
    *,
    brand_name: str,
    stage: str = "inquiry",
    handles: list[str] | None = None,
    emails: list[str] | None = None,
    platform: str | None = None,
    deal_intent: str | None = None,
) -> dict[str, Any] | None:
    """Insert a new deal row. Returns the row or None on failure. The
    caller should prefer `find_or_create_deal` — bare create is only for
    tests and manual seeding."""
    creator_uid = safe_uuid(creator_id)
    if not creator_uid:
        return None
    if stage not in STAGES:
        logger.info("babyg_deals.create_deal_bad_stage stage=%s", stage)
        return None
    brand = (brand_name or "").strip()
    if not brand:
        return None
    now = _now_iso()
    row: dict[str, Any] = {
        "creator_id": creator_uid,
        "brand_name": brand,
        "stage": stage,
        "handles": list(handles or []),
        "emails": list(emails or []),
        "first_touch_at": now,
        "last_touch_at": now,
        "notes": {},
    }
    if platform:
        row["platform"] = platform
    if deal_intent:
        row.setdefault("notes", {})["initial_intent"] = deal_intent
    try:
        result = (
            _service().table("babyg_memory_deals").insert(row).execute()
        )
        rows = result.data or []
        return rows[0] if rows else None
    except Exception:
        logger.info("babyg_deals.create_deal_failed", exc_info=True)
        return None


def find_or_create_deal(
    creator_id: str,
    *,
    brand_name: str,
    handles: list[str] | None = None,
    emails: list[str] | None = None,
    platform: str | None = None,
) -> dict[str, Any] | None:
    """The Phase 5 entry point. On the first brand touch, opens a new
    deal at stage=inquiry. On subsequent touches within the 24-hour
    window, returns the existing open deal. If the last deal against
    that brand is terminal (declined / cancelled / paid), a new deal is
    opened — a declined brand coming back is a fresh conversation.
    """
    open_deal = find_open_deal(creator_id, brand_name=brand_name)
    if open_deal is not None:
        # Merge in any newly seen handles / emails so the same deal
        # attracts future touches even if the brand shows up on a
        # different surface next time.
        new_handles = list(open_deal.get("handles") or [])
        for h in handles or []:
            if h and h not in new_handles:
                new_handles.append(h)
        new_emails = list(open_deal.get("emails") or [])
        for e in emails or []:
            if e and e not in new_emails:
                new_emails.append(e)
        if new_handles != (open_deal.get("handles") or []) or new_emails != (
            open_deal.get("emails") or []
        ):
            try:
                (
                    _service()
                    .table("babyg_memory_deals")
                    .update(
                        {
                            "handles": new_handles,
                            "emails": new_emails,
                            "updated_at": _now_iso(),
                        }
                    )
                    .eq("id", open_deal["id"])
                    .execute()
                )
                open_deal["handles"] = new_handles
                open_deal["emails"] = new_emails
            except Exception:
                logger.info("babyg_deals.merge_identity_failed", exc_info=True)
        return open_deal
    return create_deal(
        creator_id,
        brand_name=brand_name,
        handles=handles,
        emails=emails,
        platform=platform,
    )


# ---- touchpoints ----------------------------------------------------------


def add_touchpoint(
    deal_id: str,
    creator_id: str,
    *,
    kind: str,
    summary: str,
    direction: str | None = None,
    source_id: str | None = None,
    stated_amount_dollars: float | int | None = None,
    occurred_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Append one touchpoint to a deal and bump the deal's
    last_touch_at. Terminal deals refuse new touchpoints — the last
    move on a declined deal is the decline itself."""
    deal_uid = safe_uuid(deal_id)
    creator_uid = safe_uuid(creator_id)
    if not deal_uid or not creator_uid:
        return None
    if kind not in {
        "dm_message",
        "email_message",
        "calendar_event",
        "contract_pdf",
        "action_proposal",
        "note",
    }:
        logger.info("babyg_deals.touchpoint_bad_kind kind=%s", kind)
        return None
    deal = get_deal(deal_id, creator_id=creator_id)
    if deal is None:
        return None
    if is_terminal(str(deal.get("stage") or "")):
        logger.info(
            "babyg_deals.touchpoint_refused_terminal deal=%s stage=%s",
            deal_id,
            deal.get("stage"),
        )
        return None
    when = (occurred_at or datetime.now(UTC)).isoformat()
    row: dict[str, Any] = {
        "deal_id": deal_uid,
        "creator_id": creator_uid,
        "kind": kind,
        "summary": (summary or "")[:1000] or None,
        "occurred_at": when,
    }
    if direction in {"inbound", "outbound", "internal"}:
        row["direction"] = direction
    src = safe_uuid(source_id) if source_id else None
    if src:
        row["source_id"] = src
    cents = _dollars_to_cents(stated_amount_dollars)
    if cents is not None:
        row["stated_amount_cents"] = cents
    try:
        result = (
            _service()
            .table("babyg_memory_deal_touchpoints")
            .insert(row)
            .execute()
        )
        rows = result.data or []
        inserted = rows[0] if rows else None
    except Exception:
        logger.info("babyg_deals.touchpoint_insert_failed", exc_info=True)
        return None
    try:
        (
            _service()
            .table("babyg_memory_deals")
            .update({"last_touch_at": when, "updated_at": when})
            .eq("id", deal_uid)
            .execute()
        )
    except Exception:
        logger.info("babyg_deals.bump_last_touch_failed", exc_info=True)
    return inserted


# ---- stage transitions ----------------------------------------------------


def update_stage(
    deal_id: str,
    to_stage: str,
    *,
    creator_id: str,
    agreed_amount_dollars: float | int | None = None,
    paid_amount_dollars: float | int | None = None,
) -> dict[str, Any] | None:
    """Move a deal to `to_stage`. Refused if the current stage does not
    allow it, or if the deal is already terminal. Money amounts, when
    given, are stored as int cents."""
    if to_stage not in STAGES:
        logger.info("babyg_deals.update_stage_unknown to=%s", to_stage)
        return None
    deal = get_deal(deal_id, creator_id=creator_id)
    if deal is None:
        return None
    from_stage = str(deal.get("stage") or "")
    if not can_transition(from_stage, to_stage):
        logger.info(
            "babyg_deals.update_stage_refused from=%s to=%s deal=%s",
            from_stage,
            to_stage,
            deal_id,
        )
        return None
    updates: dict[str, Any] = {
        "stage": to_stage,
        "updated_at": _now_iso(),
    }
    agreed_cents = _dollars_to_cents(agreed_amount_dollars)
    if agreed_cents is not None:
        updates["agreed_amount_cents"] = agreed_cents
    paid_cents = _dollars_to_cents(paid_amount_dollars)
    if paid_cents is not None:
        updates["paid_amount_cents"] = paid_cents
    try:
        (
            _service()
            .table("babyg_memory_deals")
            .update(updates)
            .eq("id", deal["id"])
            .execute()
        )
        deal.update(updates)
        return deal
    except Exception:
        logger.info("babyg_deals.update_stage_write_failed", exc_info=True)
        return None


# ---- reads ----------------------------------------------------------------


def get_deal(
    deal_id: str, *, creator_id: str
) -> dict[str, Any] | None:
    """Fetch one deal scoped by creator_id. Returns None if not found
    or the deal belongs to someone else."""
    deal_uid = safe_uuid(deal_id)
    creator_uid = safe_uuid(creator_id)
    if not deal_uid or not creator_uid:
        return None
    try:
        rows = list(
            _service()
            .table("babyg_memory_deals")
            .select("*")
            .eq("id", deal_uid)
            .eq("creator_id", creator_uid)
            .limit(1)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.info("babyg_deals.get_deal_failed", exc_info=True)
        return None
    return rows[0] if rows else None


def list_deals(
    creator_id: str,
    *,
    stage: str | None = None,
    active_only: bool = False,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List deals for the creator, most recently touched first.
    `active_only=True` filters out terminal stages (paid/declined/
    cancelled) so 'what am I working on right now' returns cleanly."""
    creator_uid = safe_uuid(creator_id)
    if not creator_uid:
        return []
    limit = max(1, min(int(limit or 20), 100))
    try:
        query = (
            _service()
            .table("babyg_memory_deals")
            .select("*")
            .eq("creator_id", creator_uid)
            .order("last_touch_at", desc=True)
            .limit(limit)
        )
        if stage and stage in STAGES:
            query = query.eq("stage", stage)
        rows = list(query.execute().data or [])
    except Exception:
        logger.info("babyg_deals.list_deals_failed", exc_info=True)
        return []
    if active_only:
        rows = [r for r in rows if str(r.get("stage") or "") not in TERMINAL_STAGES]
    return rows


def list_touchpoints(
    deal_id: str,
    *,
    creator_id: str,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Return the touchpoints on a deal, most recent first."""
    deal_uid = safe_uuid(deal_id)
    creator_uid = safe_uuid(creator_id)
    if not deal_uid or not creator_uid:
        return []
    limit = max(1, min(int(limit or 20), 200))
    try:
        rows = list(
            _service()
            .table("babyg_memory_deal_touchpoints")
            .select("*")
            .eq("deal_id", deal_uid)
            .eq("creator_id", creator_uid)
            .order("occurred_at", desc=True)
            .limit(limit)
            .execute()
            .data
            or []
        )
    except Exception:
        logger.info("babyg_deals.list_touchpoints_failed", exc_info=True)
        return []
    return rows
