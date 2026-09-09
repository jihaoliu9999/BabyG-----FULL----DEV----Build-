"""Read-only deal presentation helpers for manager destinations."""

from __future__ import annotations

from typing import Any


def list_view(deals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_deal_view(deal, touchpoints=[]) for deal in deals]


def detail_view(
    deal: dict[str, Any], touchpoints: list[dict[str, Any]]
) -> dict[str, Any]:
    return _deal_view(deal, touchpoints=touchpoints)


def _deal_view(
    deal: dict[str, Any], *, touchpoints: list[dict[str, Any]]
) -> dict[str, Any]:
    current_offer = _latest_stated_amount(touchpoints)
    return {
        "raw": deal,
        "id": deal.get("id"),
        "brand_name": deal.get("brand_name") or "brand",
        "stage": str(deal.get("stage") or "inquiry").replace("_", " "),
        "stage_key": str(deal.get("stage") or "inquiry"),
        "platform": deal.get("platform") or "not set",
        "first_touch_at": deal.get("first_touch_at"),
        "last_touch_at": deal.get("last_touch_at"),
        "current_offer": _money(current_offer),
        "agreed_amount": _money(deal.get("agreed_amount_cents")),
        "paid_amount": _money(deal.get("paid_amount_cents")),
        "term_items": _term_items(deal),
        "recommendation": _recommendation(str(deal.get("stage") or "")),
    }


def _term_items(deal: dict[str, Any]) -> list[dict[str, str]]:
    items = [
        {"label": "deliverables", "value": _listish(deal.get("deliverables"))},
        {"label": "usage", "value": _mappingish(deal.get("usage_rights"))},
        {"label": "exclusivity", "value": _text(deal.get("exclusivity_notes"))},
        {"label": "deadline", "value": _text(deal.get("deadline"))},
        {"label": "payment", "value": _text(deal.get("payment_terms"))},
    ]
    return [
        {"label": item["label"], "value": item["value"] or "not set"}
        for item in items
    ]


def _latest_stated_amount(touchpoints: list[dict[str, Any]]) -> Any:
    for point in touchpoints:
        value = point.get("stated_amount_cents")
        if value is not None:
            return value
    return None


def _money(cents: Any) -> str:
    if cents is None:
        return "not set"
    try:
        dollars = float(cents) / 100
    except (TypeError, ValueError):
        return "not set"
    if dollars.is_integer():
        return f"${int(dollars):,}"
    return f"${dollars:,.2f}"


def _listish(value: Any) -> str:
    if isinstance(value, list):
        clean = [str(item).strip() for item in value if str(item).strip()]
        return ", ".join(clean[:6])
    return _text(value)


def _mappingish(value: Any) -> str:
    if isinstance(value, dict):
        parts = [
            f"{str(key).replace('_', ' ')}: {val}"
            for key, val in value.items()
            if val not in (None, "", [], {})
        ]
        return "; ".join(parts[:6])
    return _text(value)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())[:240]


def _recommendation(stage: str) -> str:
    if stage in {"inquiry", "waiting_on_terms"}:
        return "Clarify deliverables, usage rights, campaign length, exclusivity, timeline, and budget before quoting."
    if stage == "negotiating":
        return "Keep the counter tied to scope, rights, deadline, revisions, and payment timing."
    if stage == "accepted":
        return "Confirm production deadline, approval window, posting window, and payment path before work starts."
    if stage == "delivered":
        return "Track payment status and follow up if money is overdue."
    if stage == "payment_pending":
        return "Follow up on payment with the agreed amount and delivery date attached."
    return "Review the latest real touchpoint before changing stage or sending a reply."
