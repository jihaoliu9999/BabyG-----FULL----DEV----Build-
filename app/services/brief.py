"""babyg Brief — aggregate manager-worthy business items for the
dedicated ``/creator/brief`` page.

This service is a READ-ONLY aggregator over persisted state. It
introduces no new tables, no new writes, no new provider fanout,
and no new schema. Everything comes from the same rows the rest
of babyg's manager UI already reads:

* ``action_proposals`` (rows the agent has staged for the creator to
  confirm — Gmail send / send-draft, calendar create, etc.). Reuses
  ``app.services.action_proposals.list_pending_for_user``.
* ``notifications`` (manager alerts, connection requests, booking
  reminders, IG-tagged new_dm rows). Reuses
  ``app.services.notifications.list_for_user`` and the extra manager
  columns added by migration ``0040_manager_notifications.sql``:
  ``priority``, ``source_provider``, ``source_event_id``,
  ``source_thread_id``, ``underlying_type``, ``underlying_id``,
  ``metadata``, ``archived_at``.

Every item is a plain dict view-model — the template renders it
verbatim. No provider calls, no LLM calls, no live Meta/Gmail
fanout at Brief render time.

Lifecycle mapping to the spec's needs-you / in-progress / done:

* Gmail action_proposal ``pending``  → ``needs_you``
* Gmail action_proposal ``executing`` → ``in_progress``
* Gmail action_proposal ``executed``  → ``done`` (surfaced only in
  the recently-done section, if at all; not part of the primary
  needs-you list)
* Manager notification with ``is_read=false`` → ``needs_you``
* Manager notification with ``is_read=true``  → ``in_progress``
* Any notification with ``archived_at`` set → excluded from Brief
* Any action_proposal with a past ``expires_at`` → excluded

The one-word states above are the ONLY user-facing status labels
per the product spec. No "handled", "processed", "signal", or
manager-jargon terminology.

Instagram-specific hard rule: ``send reply`` is never offered on
an IG item, no matter what state the underlying evaluation is in.
The product does not send IG DMs from the Brief. See
``_actions_for_instagram_notification``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast

from app.services import action_proposals, notifications

logger = logging.getLogger(__name__)


BriefState = Literal["needs_you", "in_progress", "done"]
BriefSource = Literal["gmail", "instagram", "babyg", "calendar", "system"]

# The primary Brief list caps at this many rows. The rest sit in the
# "in progress" and "recently done" secondary sections. This keeps
# the page scannable on 320px viewports without hiding real state.
BRIEF_PRIMARY_MAX = 20

# Home preview cap per spec section 11. The Home carousel currently
# renders three rows; the Brief page's "view all" is the deeper list.
HOME_PREVIEW_MAX = 3

_PRIORITY_ORDER = {"urgent": 0, "high": 1, "normal": 2, "low": 3}

_ACTION_TYPE_SOURCE: dict[str, BriefSource] = {
    "gmail.send_email": "gmail",
    "gmail.send_draft": "gmail",
    "gmail.create_draft": "gmail",
    "gmail.reply": "gmail",
    "calendar.create_hold": "calendar",
    "calendar.create": "calendar",
    "calendar.update": "calendar",
    "instagram.send_dm": "instagram",  # kept for classification; NEVER surfaced as a send action
}


def build_brief(user_id: str) -> dict[str, Any]:
    """Return the full brief view model for the ``/creator/brief``
    page. Never raises — a Supabase blip yields empty lists and the
    template renders the calm empty state.
    """
    items: list[dict[str, Any]] = []
    items.extend(_items_from_action_proposals(user_id))
    items.extend(_items_from_notifications(user_id))

    # Group multiple real events belonging to the same business
    # matter into one evolving Brief item (spec §3). Prefers
    # ``source_thread_id`` when the underlying rows share one, then
    # ``(source_provider, underlying_type, underlying_id)``. The
    # standalone Brief row id is the final fallback so nothing is
    # lost when no thread identity exists.
    items = _group_into_matters(items)

    # Rank by (state, priority, created_at desc). needs-you rows
    # come first; within a state, urgent > high > normal > low;
    # within a priority, newest first.
    items.sort(key=_rank_key)
    items = items[:BRIEF_PRIMARY_MAX]

    needs_you = [it for it in items if it["state"] == "needs_you"]
    in_progress = [it for it in items if it["state"] == "in_progress"]

    return {
        "needs_you": needs_you,
        "in_progress": in_progress,
        "empty": not (needs_you or in_progress),
    }


def home_preview_rows(user_id: str) -> list[dict[str, Any]]:
    """Compact rows adapted for the Home Brief carousel.

    Home consumes the SAME aggregation as ``/creator/brief`` per
    spec Pass 2 §1 — no separate intelligence system. Returned rows
    match the shape ``creator/dashboard.html`` already renders:
    ``{slot, title, detail, href}``. Adapter kept in this module so
    the Home template can stay a pure renderer.
    """
    view = build_brief(user_id)
    top = view["needs_you"][:HOME_PREVIEW_MAX]
    return [_to_home_row(item) for item in top]


def home_preview_items(user_id: str) -> list[dict[str, Any]]:
    """Compact list of up to HOME_PREVIEW_MAX needs-you Brief items,
    ranked by the same key. Kept for callers that need the full
    Brief item shape (icons, actions) rather than the Home-flattened
    row shape."""
    view = build_brief(user_id)
    return view["needs_you"][:HOME_PREVIEW_MAX]


# ---------------------------------------------------------------------------
# Item builders
# ---------------------------------------------------------------------------


def _items_from_action_proposals(user_id: str) -> list[dict[str, Any]]:
    """Turn each pending action_proposal into a Brief item.

    Reuses the existing ``list_pending_for_user`` which already
    filters out expired rows. Never hits a provider — the
    ``preview`` jsonb column carries the recipient / amount /
    summary the agent staged.
    """
    try:
        rows = action_proposals.list_pending_for_user(
            user_id=user_id, limit=BRIEF_PRIMARY_MAX
        )
    except Exception:
        logger.exception("brief.list_action_proposals.failed user=%s", user_id)
        return []
    return [_item_from_proposal(row) for row in rows if isinstance(row, dict)]


def _item_from_proposal(row: dict[str, Any]) -> dict[str, Any]:
    preview = row.get("preview") or {}
    if not isinstance(preview, dict):
        preview = {}
    source = _source_from_proposal(row, preview)
    summary = _first_nonempty(
        preview.get("summary"),
        preview.get("brief"),
        preview.get("subject"),
    )
    recommendation = _first_nonempty(
        preview.get("recommendation"),
        preview.get("draft_summary"),
        preview.get("body_preview"),
        preview.get("body"),
    )
    what = _shape_what_happened(
        source,
        summary,
        preview,
        kind="proposal",
    )
    # Business state derives from the real ``action_proposals.status``
    # column (migration 0012). This is the ONLY code path where a
    # Brief item can be ``in_progress`` — babyg has actually kicked
    # off the executor and is waiting on the provider round-trip.
    #   pending    → needs_you  (user must approve)
    #   confirmed  → in_progress (executor about to run)
    #   executing  → in_progress (executor mid-flight)
    #   any other  → needs_you  (list_pending_for_user filters
    #                            executed/failed/cancelled/expired
    #                            already; this is the safe default)
    raw_status = str(row.get("status") or "pending").lower()
    if raw_status in {"confirmed", "executing"}:
        state: BriefState = "in_progress"
    else:
        state = "needs_you"
    return {
        "id": f"proposal:{row.get('id')}",
        "kind": "proposal",
        "proposal_id": row.get("id"),
        "source_message_id": row.get("source_message_id"),
        "source": source,
        "source_label": _source_label(source),
        "state": state,
        # Proposals don't have a notification-style ``is_read`` — a
        # creator seeing them in the Brief IS the seen event, so
        # they're always ``seen`` in the attention sense; template
        # renders no unseen dot for them.
        "seen": True,
        "priority": "normal",
        "what_happened": what,
        "recommendation": _shorten(recommendation, 200),
        "actions": _actions_for_proposal(row, source),
        "ask_babyg_href": (
            f"/creator/bot?brief=proposal:{row.get('id')}" if row.get("id") else None
        ),
        "created_at": row.get("created_at"),
        # Proposals rarely carry a shared thread identity; if one is
        # attached via preview.thread_id we surface it for grouping,
        # else the Brief-row id is the final fallback.
        "source_thread_id": str(preview.get("thread_id") or "").strip() or None,
        "underlying_type": (
            "gmail_thread"
            if source == "gmail"
            else None
        ),
        "underlying_id": str(preview.get("thread_id") or "").strip() or None,
        "source_provider": source if source in ("gmail", "instagram") else None,
    }


def _items_from_notifications(user_id: str) -> list[dict[str, Any]]:
    """Turn each visible manager-worthy notification row into a
    Brief item. Excludes archived rows and rows that lack a
    ``link_path``/``metadata`` payload we can render safely."""
    try:
        rows = notifications.list_for_user(
            user_id, limit=BRIEF_PRIMARY_MAX * 2, include_archived=False
        )
    except Exception:
        logger.exception("brief.list_notifications.failed user=%s", user_id)
        return []
    return [
        item
        for row in rows
        if isinstance(row, dict) and (item := _item_from_notification(row))
    ]


def _item_from_notification(row: dict[str, Any]) -> dict[str, Any] | None:
    kind = str(row.get("kind") or "").strip()
    source = _source_from_notification(row)
    if not _is_brief_worthy(kind, source, row):
        return None
    # Pass 2 §2 lifecycle correction:
    # SEEN != IN PROGRESS. Opening a notification does not mean the
    # underlying business action has begun. Every manager-worthy
    # notification stays ``needs_you`` until the user takes a real
    # follow-through action (draft/send/archive). ``seen`` is a
    # separate attention flag rendered independently by the template.
    # Archived rows are already filtered out upstream via
    # ``notifications.list_for_user(include_archived=False)``.
    is_read = bool(row.get("is_read"))
    state: BriefState = "needs_you"
    seen: bool = is_read
    priority = str(row.get("priority") or "normal").strip().lower()
    title = _shorten(str(row.get("title") or "").strip(), 140)
    body = _shorten(str(row.get("body") or "").strip(), 200)
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    what = _shape_what_happened(source, title, metadata, kind=kind)
    # Business-matter grouping keys — the highest-value stable
    # identifier a notification row exposes for aggregation. Order
    # mirrors spec §3: source_thread_id first, then
    # (source_provider, underlying_type, underlying_id).
    source_thread_id = str(row.get("source_thread_id") or "").strip() or None
    underlying_type = str(row.get("underlying_type") or "").strip() or None
    underlying_id = str(row.get("underlying_id") or "").strip() or None
    return {
        "id": f"notif:{row.get('id')}",
        "kind": "notification",
        "notification_id": row.get("id"),
        "source": source,
        "source_label": _source_label(source),
        "state": state,
        "seen": seen,
        "priority": priority,
        "what_happened": what,
        "recommendation": body,
        "actions": _actions_for_notification(row, source),
        "ask_babyg_href": (
            f"/creator/bot?brief=notif:{row.get('id')}" if row.get("id") else None
        ),
        "created_at": row.get("created_at"),
        "link_path": str(row.get("link_path") or "").strip() or None,
        "source_thread_id": source_thread_id,
        "underlying_type": underlying_type,
        "underlying_id": underlying_id,
        "source_provider": str(row.get("source_provider") or source or "").strip().lower()
        or None,
    }


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------


def _actions_for_proposal(
    row: dict[str, Any], source: BriefSource
) -> list[dict[str, Any]]:
    """Return the compact action list for a Brief proposal item.

    Only ``send_email`` / ``send_draft`` / ``send`` actions get a
    real send button — and only when there is a ``source_message_id``
    the existing bot confirm endpoint can address. If we can't
    address the existing confirm endpoint we render an ``ask babyg``
    fallback instead of a broken button.

    Instagram send is never surfaced here even if the underlying
    action_proposal type would technically support it: this is the
    product-level restriction from spec section 8.
    """
    action_type = str(row.get("action_type") or "").strip()
    proposal_id = str(row.get("id") or "").strip()
    source_message_id = str(row.get("source_message_id") or "").strip()

    actions: list[dict[str, Any]] = []

    # Gmail send path — reuse the existing bot-messages confirm
    # endpoint. Preserves autonomy + safety gates unchanged.
    if (
        source == "gmail"
        and action_type in {"gmail.send_email", "gmail.send_draft"}
        and source_message_id
    ):
        actions.append(
            {
                "label": _gmail_send_label(action_type),
                "method": "POST",
                "endpoint": f"/creator/bot/actions/{source_message_id}/confirm",
                "style": "primary",
            }
        )

    if proposal_id:
        actions.append(
            {
                "label": "ask babyg",
                "method": "GET",
                "endpoint": f"/creator/bot?brief=proposal:{proposal_id}",
                "style": "ghost",
            }
        )
    return actions


def _actions_for_notification(
    row: dict[str, Any], source: BriefSource
) -> list[dict[str, Any]]:
    """Return the compact action list for a Brief notification item.

    Instagram notifications never expose a send action — see spec
    section 8. Every other source falls back to ``ask babyg`` and
    the underlying ``link_path`` if present.
    """
    actions: list[dict[str, Any]] = []
    link_path = str(row.get("link_path") or "").strip()
    valid_link = _valid_internal_href(link_path)

    if source == "instagram":
        # Instagram business inquiries can be drafted/reviewed but
        # never sent from the Brief. The literal link into the
        # inquiry surface uses whatever the notification's link_path
        # already resolves to (owned by the manager notification
        # pipeline, not by this Brief service).
        if valid_link:
            actions.append(
                {
                    "label": "review inquiry",
                    "method": "GET",
                    "endpoint": valid_link,
                    "style": "primary",
                }
            )
    elif source == "calendar":
        if valid_link:
            actions.append(
                {
                    "label": "view event",
                    "method": "GET",
                    "endpoint": valid_link,
                    "style": "primary",
                }
            )
    elif valid_link:
        actions.append(
            {
                "label": "review",
                "method": "GET",
                "endpoint": valid_link,
                "style": "primary",
            }
        )

    notif_id = str(row.get("id") or "").strip()
    if notif_id:
        actions.append(
            {
                "label": "ask babyg",
                "method": "GET",
                "endpoint": f"/creator/bot?brief=notif:{notif_id}",
                "style": "ghost",
            }
        )
    return actions


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_from_proposal(
    row: dict[str, Any], preview: dict[str, Any]
) -> BriefSource:
    """Resolve proposal source from persisted fields only.

    Order matches the product contract: explicit source, metadata,
    provider/action type, known relationship, then native babyg.
    """
    explicit = _known_source(row.get("source_provider"))
    if explicit:
        return explicit

    metadata_source = _source_from_metadata(preview) or _source_from_metadata(
        row.get("metadata")
    )
    if metadata_source:
        return metadata_source

    action_type = str(row.get("action_type") or "").strip()
    action_source = _ACTION_TYPE_SOURCE.get(action_type)
    if action_source:
        return action_source

    provider = str(row.get("provider") or "").strip().lower()
    if provider in {"instagram", "gmail", "calendar"}:
        return cast(BriefSource, provider)
    if provider == "google":
        if action_type.startswith("gmail."):
            return "gmail"
        if action_type.startswith("calendar."):
            return "calendar"

    return _source_from_underlying(row) or "babyg"


def _source_from_notification(row: dict[str, Any]) -> BriefSource:
    explicit = _known_source(row.get("source_provider"))
    if explicit:
        return explicit

    metadata_source = _source_from_metadata(row.get("metadata"))
    if metadata_source:
        return metadata_source

    kind = str(row.get("kind") or "").strip()
    if kind == "booking_reminder":
        return "calendar"
    if kind == "connection_request":
        return "babyg"

    return _source_from_underlying(row) or "babyg"


def _source_from_metadata(metadata: Any) -> BriefSource | None:
    if not isinstance(metadata, dict):
        return None
    for key in (
        "source_provider",
        "provider",
        "source",
        "platform",
        "channel",
    ):
        source = _known_source(metadata.get(key))
        if source:
            return source
    return None


def _source_from_underlying(row: dict[str, Any]) -> BriefSource | None:
    underlying_type = str(row.get("underlying_type") or "").strip().lower()
    underlying_id = str(row.get("underlying_id") or "").strip()
    link_path = str(row.get("link_path") or "").strip().lower()
    if underlying_type and underlying_id:
        if underlying_type.startswith("instagram_"):
            return "instagram"
        if underlying_type.startswith("gmail_"):
            return "gmail"
        if underlying_type.startswith("calendar_") or underlying_type == "booking":
            return "calendar"
        if underlying_type.startswith("dm_"):
            return "babyg"
    if link_path.startswith("/creator/instagram/"):
        return "instagram"
    if link_path.startswith("/creator/calendar"):
        return "calendar"
    if link_path.startswith("/creator/connections") or link_path.startswith(
        "/creator/network"
    ):
        return "babyg"
    return None


def _known_source(value: Any) -> BriefSource | None:
    source = str(value or "").strip().lower()
    if source in {"gmail", "instagram", "calendar", "babyg", "system"}:
        return cast(BriefSource, source)
    if source in {"google_calendar", "google-calendar"}:
        return "calendar"
    if source in {"google_gmail", "google-gmail"}:
        return "gmail"
    return None


def _valid_internal_href(value: str) -> str | None:
    href = value.strip()
    lowered = href.lower()
    if not href or href == "#" or lowered.startswith("javascript:"):
        return None
    if not href.startswith("/") or href.startswith("//"):
        return None
    return href


def _is_brief_worthy(
    kind: str, source: BriefSource, row: dict[str, Any]
) -> bool:
    """Filter noise. Only manager-worthy notifications belong on
    the Brief page — casual/generic system notices don't.

    Per spec section 12: casual DMs, jokes, generic compliments,
    provider sync noise get filtered upstream by the ingestion
    layer before a `manager_alert`/`new_dm` row is created. This
    filter is the last-line defense.
    """
    manager_kinds = {
        "manager_alert",
        "new_dm",
        "booking_reminder",
        "connection_request",
        "collab_match",
        "job_match",
        "performance_spike",
    }
    if kind not in manager_kinds:
        return False
    # A `new_dm` without an explicit source_provider is a legacy
    # native DM alert — the Brief surfaces native DMs elsewhere.
    if kind == "new_dm" and source != "instagram":
        return False
    return not (source == "instagram" and _looks_like_raw_instagram_count(row))


def _looks_like_raw_instagram_count(row: dict[str, Any]) -> bool:
    raw_metadata = row.get("metadata")
    metadata: dict[str, Any] = raw_metadata if isinstance(raw_metadata, dict) else {}
    marker = _first_nonempty(
        metadata.get("summary_type"),
        metadata.get("event_type"),
        metadata.get("category"),
    ).lower()
    if marker in {"raw_unread_count", "unread_count", "sync_count"}:
        return True
    title = str(row.get("title") or "").strip().lower()
    body = str(row.get("body") or "").strip().lower()
    text = f"{title} {body}"
    raw_tokens = (
        "new instagram dms",
        "new instagram dm",
        "unread instagram",
        "instagram unread",
        "caught ",
    )
    return any(token in text for token in raw_tokens) and not (
        row.get("source_event_id")
        or row.get("source_thread_id")
        or row.get("underlying_id")
    )


def _group_into_matters(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse events belonging to the same business matter into one
    evolving Brief item (spec Pass 2 §3).

    Grouping key priority (spec §3 hierarchy):
      1. ``source_thread_id`` — Instagram DM thread row uuid, the
         strongest available identity for a live conversation.
      2. ``(source_provider, underlying_type, underlying_id)`` — used
         by Gmail proposals that stash the gmail thread id in
         ``preview.thread_id`` and by any future provider that
         populates the underlying pair.
      3. The Brief row's own id — final fallback so nothing is
         lost when no thread identity is available.

    Within a group we keep the newest row's summary/recommendation
    (spec §3 "use the latest/current meaningful state") but promote
    the highest state (needs_you > in_progress) and highest priority
    from the group. The kept row's ``ask_babyg_href`` uses the
    newest row's own key so the manager loads the freshest topic.
    """
    order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        key = _matter_key(item)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(item)
    out: list[dict[str, Any]] = []
    for key in order:
        bucket = groups[key]
        if len(bucket) == 1:
            out.append(bucket[0])
            continue
        # Sort newest first — the top-of-list row donates the
        # current summary/recommendation/actions.
        bucket.sort(key=lambda it: str(it.get("created_at") or ""), reverse=True)
        head = dict(bucket[0])
        # Promote highest state: needs_you beats in_progress.
        states = {it.get("state") for it in bucket}
        head["state"] = (
            "needs_you" if "needs_you" in states else head.get("state")
        )
        # Promote highest priority present.
        head["priority"] = _highest_priority(
            [str(it.get("priority") or "normal") for it in bucket]
        )
        # Preserve whether any event in the group is still unseen.
        head["seen"] = all(bool(it.get("seen")) for it in bucket)
        # Preserve the count so the template can show "+N related"
        # if it wants to. No fake activity numbers — only truthful
        # group size.
        head["matter_event_count"] = len(bucket)
        out.append(head)
    return out


def _matter_key(item: dict[str, Any]) -> str:
    """Return the stable grouping key for one Brief item.

    See ``_group_into_matters`` for the priority order.
    """
    thread_id = str(item.get("source_thread_id") or "").strip()
    if thread_id:
        return f"thread:{item.get('source', '')}:{thread_id}"
    provider = str(item.get("source_provider") or "").strip().lower()
    utype = str(item.get("underlying_type") or "").strip()
    uid = str(item.get("underlying_id") or "").strip()
    if provider and utype and uid:
        return f"underlying:{provider}:{utype}:{uid}"
    # Final fallback: the Brief item's own id, which guarantees no
    # unrelated matters get accidentally merged.
    return f"row:{item.get('id', '')}"


def _highest_priority(values: list[str]) -> str:
    """Return the strongest priority label present in ``values``."""
    order = _PRIORITY_ORDER
    best = "normal"
    best_rank = order.get(best, 2)
    for value in values:
        v = value.lower()
        r = order.get(v, best_rank)
        if r < best_rank:
            best = v
            best_rank = r
    return best


def _to_home_row(item: dict[str, Any]) -> dict[str, Any]:
    """Adapt a Brief item to the shape ``dashboard.html`` renders.

    Home is a compressed preview: single strong title (the concise
    ``what_happened``), a small source-and-category label, and a link
    that lands on ``/creator/brief`` where the full matter lives.
    Icons map to the same visual slots ``brief.html`` uses.

    Home never renders send buttons — those live only on the full
    Brief page. Tapping a Home preview row always opens the full
    Brief where the literal action lives (spec Pass 2 §1).
    """
    source = str(item.get("source") or "babyg")
    slot = source if source in {"gmail", "instagram", "calendar"} else "babyg"
    what = str(item.get("what_happened") or "").strip()
    detail = _home_row_detail(item)
    return {
        "slot": slot,
        "title": what[:100],
        "detail": detail,
        "href": "/creator/brief",
    }


def _home_row_detail(item: dict[str, Any]) -> str:
    """Return the small caption Home renders under the strong title.

    Uses literal category verbs (spec §21 vocabulary): ``new inquiry``,
    ``reply ready``, ``counter ready``, ``follow-up due``. Falls back
    to the source label alone when no more specific verb applies.
    """
    source = str(item.get("source") or "babyg")
    kind = str(item.get("kind") or "")
    source_label = str(item.get("source_label") or source)
    if kind == "proposal":
        # Proposal source implies babyg has a draft or counter
        # already prepared for the creator to review/send.
        return f"{source_label} · reply ready"
    if source == "instagram":
        return f"{source_label} · new inquiry"
    if source == "gmail":
        return f"{source_label} · needs a decision"
    if source == "calendar":
        return f"{source_label} · needs a decision"
    return source_label


def _rank_key(item: dict[str, Any]) -> tuple[int, int, str]:
    state_rank = {"needs_you": 0, "in_progress": 1, "done": 2}
    priority_rank = _PRIORITY_ORDER.get(
        str(item.get("priority") or "normal").lower(), 2
    )
    # Newer first among ties — sort by negated iso string ordering.
    created_at = str(item.get("created_at") or "")
    return (state_rank.get(str(item.get("state") or ""), 3), priority_rank, _iso_desc(created_at))


def _iso_desc(iso: str) -> str:
    """Return a string that sorts descending by date (newest first)."""
    # Simple invariant: newer iso strings sort GREATER, so negate by
    # returning the character-flipped complement. Cheaper than a
    # datetime parse per row.
    return "".join(chr(0x10FFFF - ord(ch)) for ch in iso[:32]) if iso else ""


def _shorten(text: str | None, cap: int) -> str:
    if not text:
        return ""
    clean = " ".join(str(text).split())
    return clean if len(clean) <= cap else clean[: cap - 1].rstrip() + "…"


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _shape_what_happened(
    source: BriefSource,
    summary: str,
    metadata: Any,
    *,
    kind: str | None = None,
) -> str:
    """Return the 'what happened' one-liner. Prefers the concrete
    summary from the source. Never fabricates: an empty summary
    yields a calm generic string that still tells the user *which
    channel* the item came from."""
    meta = metadata if isinstance(metadata, dict) else {}
    text = _shorten(_specific_summary(source, summary, meta, kind), 140)
    if text:
        return text
    fallback = {
        "gmail": "a new gmail thread needs a decision",
        "instagram": "a new instagram inquiry needs a look",
        "calendar": "a calendar update needs attention",
        "babyg": "a babyg update needs review",
        "system": "an update is waiting",
    }
    return fallback[source]


def _specific_summary(
    source: BriefSource,
    summary: str,
    metadata: dict[str, Any],
    kind: str | None,
) -> str:
    if kind == "connection_request":
        if _is_generic_connection_summary(summary):
            name = _connection_identity(metadata)
            return f"New connection request from {name}" if name else "New connection request"
        return summary
    if _looks_like_raw_processing_count(summary):
        return ""
    if source == "instagram":
        return _instagram_summary(summary, metadata)
    if source == "gmail":
        return _first_nonempty(
            summary,
            metadata.get("summary"),
            metadata.get("subject"),
            metadata.get("sender_name"),
            metadata.get("from_name"),
        )
    if source == "calendar":
        return _first_nonempty(
            summary,
            metadata.get("event_title"),
            metadata.get("title"),
            metadata.get("summary"),
        )
    return summary


def _instagram_summary(summary: str, metadata: dict[str, Any]) -> str:
    if summary:
        return summary
    peer = _first_nonempty(
        metadata.get("peer_username"),
        metadata.get("username"),
        metadata.get("sender_username"),
        metadata.get("counterparty"),
    )
    if peer:
        peer_label = peer if peer.startswith("@") else f"@{peer}"
        attachment_types = metadata.get("attachment_types")
        if isinstance(attachment_types, list) and attachment_types:
            label = str(attachment_types[0] or "").strip().lower()
            if label:
                return f"{peer_label} shared an instagram {label}"
        return f"{peer_label} sent an instagram message"
    return ""


def _connection_identity(metadata: dict[str, Any]) -> str:
    return _first_nonempty(
        metadata.get("requester_name"),
        metadata.get("display_name"),
        metadata.get("from_name"),
        metadata.get("sender_name"),
        metadata.get("peer_name"),
        metadata.get("counterparty"),
    )


def _is_generic_connection_summary(summary: str) -> bool:
    clean = summary.strip().lower().rstrip(".")
    return clean in {
        "",
        "someone wants to connect",
        "new connection request",
        "connection request",
    }


def _looks_like_raw_processing_count(summary: str) -> bool:
    clean = summary.strip().lower()
    return any(
        token in clean
        for token in (
            "processed ",
            "handled ",
            "caught ",
            " unread ",
            "new activity",
            "business activity",
            "something needs your attention",
        )
    )


def _source_label(source: BriefSource) -> str:
    labels = {
        "gmail": "GMAIL",
        "instagram": "INSTAGRAM",
        "babyg": "BABYG",
        "calendar": "CALENDAR",
        "system": "BABYG",
    }
    return labels.get(source, "BABYG")


def _gmail_send_label(action_type: str) -> str:
    if action_type == "gmail.send_draft":
        return "send reply"
    return "send reply"


# ---------------------------------------------------------------------------
# Context resolution — powers ?brief=<id> on /creator/bot.
# ---------------------------------------------------------------------------


def resolve_brief_context(
    *, brief_key: str, user_id: str
) -> dict[str, Any] | None:
    """Look up the Brief item identified by ``brief_key`` (e.g.
    ``proposal:<uuid>`` or ``notif:<uuid>``) and return a small
    dict the manager route can pass into the assistant prompt +
    chip strip.

    Every lookup is owner-scoped so a copied URL can never surface
    another creator's item. Returns None on any decode/lookup
    failure; the manager route falls through to its normal
    behavior.
    """
    if not brief_key or ":" not in brief_key:
        return None
    kind, _, raw_id = brief_key.partition(":")
    raw_id = raw_id.strip()
    if not raw_id:
        return None
    if kind == "proposal":
        try:
            row = action_proposals.get_for_user(
                proposal_id=raw_id, user_id=user_id
            )
        except Exception:
            logger.exception("brief.resolve_proposal.failed user=%s", user_id)
            return None
        if not row:
            return None
        preview = row.get("preview") or {}
        preview = preview if isinstance(preview, dict) else {}
        source = _source_from_proposal(row, preview)
        summary = _first_nonempty(
            preview.get("summary"),
            preview.get("brief"),
            preview.get("subject"),
        )
        return {
            "kind": "proposal",
            "id": str(row.get("id")),
            "source": source,
            "source_label": _source_label(source),
            "summary": _shape_what_happened(
                source,
                summary,
                preview,
                kind="proposal",
            ),
            "recommendation": _shorten(
                _first_nonempty(
                    preview.get("recommendation"),
                    preview.get("draft_summary"),
                    preview.get("body_preview"),
                ),
                200,
            ),
        }
    if kind == "notif":
        try:
            row = _get_notification_for_user(
                notification_id=raw_id, user_id=user_id
            )
        except Exception:
            logger.exception(
                "brief.resolve_notification.failed user=%s", user_id
            )
            return None
        if not row:
            return None
        notif_source = _source_from_notification(row)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        title = _shorten(str(row.get("title") or "").strip(), 140)
        return {
            "kind": "notification",
            "id": str(row.get("id")),
            "source": notif_source,
            "source_label": _source_label(notif_source),
            "summary": _shape_what_happened(
                notif_source,
                title,
                metadata,
                kind=str(row.get("kind") or ""),
            ),
            "recommendation": _shorten(str(row.get("body") or ""), 200),
        }
    return None


def _get_notification_for_user(
    *, notification_id: str, user_id: str
) -> dict[str, Any] | None:
    """Owner-scoped notification lookup used only by the ?brief=<id>
    context resolver. Split out so tests can monkeypatch it.

    Kept local to the brief service so we don't grow the
    notifications public API just for context resolution. Uses
    ``notifications.list_for_user`` (which already scopes by
    ``user_id``) and filters in Python — the single row is O(1)
    from the request's perspective because the row is already
    fetched for the Brief render higher up the request. In this
    lookup path it's a cheap standalone read, on the order of one
    postgrest call. The Brief route is a single page render, so
    the additional read is bounded.
    """
    rows = notifications.list_for_user(user_id, limit=200, include_archived=False)
    target = str(notification_id or "").strip()
    for row in rows:
        if isinstance(row, dict) and str(row.get("id") or "") == target:
            return row
    return None


# Late import guard — kept so a test file can patch the module-level
# ``datetime`` helper without pulling in the whole app.
_ = (datetime, timedelta, UTC)
