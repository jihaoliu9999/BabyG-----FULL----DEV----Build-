"""babyg Manager events for the deal payment lifecycle (Step 7A).

Grounded, fixed sentences about real state changes -- no LLM, no analysis:

* ``deal.awaiting_payment`` -- recorded right after an offer is accepted
  and its deal exists. Payer: "Your deal with X is ready for payment."
  Recipient: "Your deal with X is awaiting payment."
* ``deal.funded`` -- recorded after a webhook-confirmed payment funded the
  deal. Both: "Payment for your deal with X is confirmed. The deal is
  funded."

Each event is one ``notifications`` row per party (kind ``manager_alert``,
the existing manager architecture), keyed by
``source_event_id = "<event>:<deal_id>"`` so a webhook retry or a repeated
call can never record it twice. Rows are surfaced in the babyg Manager
(creators: the pinned /creator/bot page; brands: the top of /brand/dm) and
NEVER written into human-to-human DM threads.

Actions are named in ``metadata.actions`` and resolved here from an
allowlist, so later offer events can add Accept / Counter / Decline
without changing the storage shape. Today there is only "View deal".

Recording never raises: a Manager event failing must not break an accept
or a payment webhook. Failures are logged.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.services import job_deals, notifications

logger = logging.getLogger(__name__)

SOURCE_PROVIDER = "babyg"
UNDERLYING_TYPE = "creator_job_deal"
KIND = "manager_alert"
AWAITING_PAYMENT = "deal.awaiting_payment"
FUNDED = "deal.funded"

# action key -> button label. The link is the event's own link_path.
ACTION_LABELS = {"view_deal": "View deal"}
_DEAL_PATH_PREFIXES = ("/creator/dm/deals/", "/brand/dm/deals/")
RECENT_DAYS = 14
_RECENT_SCAN = 30


def deal_path(deal_id: str, *, brand: bool) -> str:
    return f"{'/brand' if brand else '/creator'}/dm/deals/{deal_id}"


def _parties(deal_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """(the deal as its payer sees it, the deal as its recipient sees it),
    both through the same party-scoped reads the Deal page uses."""
    result = (
        supabase_client.get_service_client()
        .table("creator_job_deals")
        .select("id,poster_user_id,applicant_user_id")
        .eq("id", deal_id)
        .limit(1)
        .execute()
    )
    rows = getattr(result, "data", None) or []
    if not rows:
        return None
    as_payer = job_deals.get_for_user(deal_id, str(rows[0].get("poster_user_id") or ""))
    as_recipient = job_deals.get_for_user(deal_id, str(rows[0].get("applicant_user_id") or ""))
    if as_payer is None or as_recipient is None:
        return None
    return as_payer, as_recipient


def _record(deal_id: str, event: str) -> int:
    did = safe_uuid(deal_id)
    if not did:
        return 0
    try:
        parties = _parties(did)
    except Exception:
        logger.exception("deal_events.read_failed event=%s", event)
        return 0
    if parties is None:
        logger.warning("deal_events.deal_unavailable event=%s", event)
        return 0
    as_payer, as_recipient = parties
    payer_is_brand = (as_recipient.get("other") or {}).get("kind") == "brand"
    payer_sees = (as_payer.get("other") or {}).get("name") or "your creator"
    recipient_sees = (as_recipient.get("other") or {}).get("name") or "your partner"
    if event == AWAITING_PAYMENT:
        payer_title = f"Your deal with {payer_sees} is ready for payment."
        recipient_title = f"Your deal with {recipient_sees} is awaiting payment."
    else:
        payer_title = f"Payment for your deal with {payer_sees} is confirmed. The deal is funded."
        recipient_title = f"Payment for your deal with {recipient_sees} is confirmed. The deal is funded."
    recorded = 0
    for user_id, title, path in (
        (str(as_payer["poster_user_id"]), payer_title, deal_path(did, brand=payer_is_brand)),
        (str(as_recipient["applicant_user_id"]), recipient_title, deal_path(did, brand=False)),
    ):
        try:
            recorded += notifications.create_once(
                user_id=user_id,
                kind=KIND,
                title=title,
                link_path=path,
                source_provider=SOURCE_PROVIDER,
                source_event_id=f"{event}:{did}",
                underlying_type=UNDERLYING_TYPE,
                underlying_id=did,
                metadata={"event": event, "matter_type": "deal", "actions": ["view_deal"]},
            )
        except Exception:
            logger.exception("deal_events.record_failed event=%s", event)
    return recorded


def record_awaiting_payment(deal_id: str) -> int:
    """After an accept created the deal. Returns rows newly recorded."""
    return _record(deal_id, AWAITING_PAYMENT)


def record_funded(deal_id: str) -> int:
    """After a webhook-confirmed payment funded the deal."""
    return _record(deal_id, FUNDED)


def list_recent(user_id: str, *, limit: int) -> list[dict[str, Any]]:
    """The newest event per deal from the last RECENT_DAYS days, newest
    first, for the Manager surfaces. A funded event supersedes the same
    deal's awaiting-payment event. Read failures render as no updates."""
    uid = safe_uuid(user_id)
    if not uid or limit <= 0:
        return []
    since = (datetime.now(UTC) - timedelta(days=RECENT_DAYS)).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("notifications")
            .select("id,title,link_path,underlying_id,metadata,created_at")
            .eq("user_id", uid)
            .eq("kind", KIND)
            .eq("source_provider", SOURCE_PROVIDER)
            .eq("underlying_type", UNDERLYING_TYPE)
            .is_("archived_at", "null")
            .gte("created_at", since)
            .order("created_at", desc=True)
            .limit(_RECENT_SCAN)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("deal_events.list_recent failed")
        return []
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in getattr(result, "data", None) or []:
        deal_id = str(row.get("underlying_id") or "")
        path = str(row.get("link_path") or "")
        if not deal_id or deal_id in seen:
            continue
        seen.add(deal_id)
        if not path.startswith(_DEAL_PATH_PREFIXES):
            continue
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        actions = [
            {"label": ACTION_LABELS[key], "path": path}
            for key in (metadata.get("actions") or [])
            if key in ACTION_LABELS
        ]
        out.append({"id": row.get("id"), "title": row.get("title") or "", "actions": actions})
        if len(out) >= limit:
            break
    return out
