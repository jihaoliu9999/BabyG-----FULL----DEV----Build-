"""Instagram DM ingestion — takes Meta's verified webhook payload
and lands it in `instagram_dm_threads` + `instagram_dm_messages`.

Called by `app.routes.webhooks._dispatch_payload` after HMAC
signature verification. The route already ack'd Meta with a 200,
so nothing here can propagate back to the network — every failure
is logged and swallowed.

## Meta payload shape (Instagram messaging webhook)

```
{
  "object": "instagram",
  "entry": [
    {
      "id": "<ig_business_account_id>",   // whose account received it
      "time": 1699999999,
      "messaging": [
        {
          "sender":    {"id": "<ig_user_id_of_peer>"},
          "recipient": {"id": "<ig_business_account_id>"},
          "timestamp": 1699999999123,
          "message": {
            "mid": "<ig_message_id>",
            "text": "hey! love your reel",
            "attachments": [{"type": "image", "payload": {"url": "..."}}]
          }
        }
      ]
    }
  ]
}
```

Outbound messages (echoes) look the same but have
`message.is_echo = true` and sender/recipient swapped. We store
both directions.

## Failure modes

- payload malformed  -> log + drop entry, keep processing others
- unknown IG account -> log at info + drop entry (creator hasn't
                         connected IG yet, or revoked)
- supabase down      -> log + drop, next webhook retry from Meta
                         will re-attempt (or slab #4's periodic
                         reconciler will fetch the missing window)

## Idempotence

The `(creator_id, ig_message_id)` unique constraint (migration
0038) makes duplicate deliveries safe at the schema level. We
upsert on conflict-do-nothing so a retry never over-writes an
already-persisted message with a fresher timestamp.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.core import supabase_client
from app.services import dm_briefs, notifications

logger = logging.getLogger(__name__)

_MANAGER_ATTACHMENT_TYPES = {
    "reel",
    "ig_reel",
    "post",
    "ig_post",
    "story_mention",
    "image",
    "video",
    "audio",
    "file",
}


def list_threads_for_creator(user_id: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """Read helper for the /creator/instagram/dms route. Threads
    newest-first, capped at `limit`. Never raises — flaky supabase
    returns []."""
    capped = max(1, min(int(limit), 100))
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_threads")
            .select(
                "id,ig_thread_id,ig_peer_user_id,peer_username,"
                "last_message_at,unread_count"
            )
            .eq("creator_id", user_id)
            .order("last_message_at", desc=True)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception("instagram_dms.list_threads_for_creator.failed user=%s", user_id)
        return []
    return list(getattr(result, "data", None) or [])


def unread_count_for_creator(user_id: str) -> int:
    """Sum of `unread_count` across every IG DM thread for this creator.

    Read helper for the home dashboard chip that says "N unread
    instagram dm(s)". Never raises — a flaky supabase returns 0
    (better to show no chip than to blow up the home render)."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_threads")
            .select("unread_count")
            .eq("creator_id", user_id)
            .gt("unread_count", 0)
            .execute()
        )
    except Exception:
        logger.exception("instagram_dms.unread_count_for_creator.failed user=%s", user_id)
        return 0
    rows = list(getattr(result, "data", None) or [])
    return sum(int(r.get("unread_count") or 0) for r in rows)


def list_messages_for_thread(
    user_id: str, thread_id: str, *, limit: int = 100
) -> list[dict[str, Any]]:
    """Read helper. Messages ascending by received_at so the UI can
    render top-to-bottom. Owner-scoped via creator_id so an operator
    query never leaks another creator's DMs."""
    capped = max(1, min(int(limit), 500))
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_messages")
            .select(
                "id,thread_id,direction,sender_ig_id,body,attachments,received_at"
            )
            .eq("creator_id", user_id)
            .eq("thread_id", thread_id)
            .order("received_at", desc=False)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception(
            "instagram_dms.list_messages_for_thread.failed user=%s thread=%s",
            user_id,
            thread_id,
        )
        return []
    return list(getattr(result, "data", None) or [])


def ingest_webhook_payload(payload: dict[str, Any]) -> dict[str, int]:
    """Top-level entrypoint. Returns a small stats dict for the
    webhook route's log line — never raises."""
    stats = {"entries": 0, "messages_ingested": 0, "dropped_no_creator": 0, "errors": 0}
    if not isinstance(payload, dict):
        logger.info("instagram_dms.payload.unsupported reason=not_object")
        return stats
    if payload.get("object") != "instagram":
        # We only handle Instagram right now. Meta uses the same
        # webhook shape for Facebook Pages / WhatsApp; ignore quietly.
        logger.info(
            "instagram_dms.payload.unsupported object=%s",
            str(payload.get("object") or "")[:40] or "missing",
        )
        return stats
    entries = payload.get("entry") or []
    if not isinstance(entries, list):
        logger.info("instagram_dms.payload.unsupported reason=entry_not_list")
        return stats
    logger.info("instagram_dms.payload.accepted entries=%s", len(entries))
    for entry in entries:
        stats["entries"] += 1
        try:
            _ingest_entry(entry, stats)
        except Exception:
            logger.exception("instagram_dms.ingest_entry.crashed")
            stats["errors"] += 1
    return stats


def _ingest_entry(entry: dict[str, Any], stats: dict[str, int]) -> None:
    if not isinstance(entry, dict):
        return
    ig_business_account_id = str(entry.get("id") or "").strip()
    if not ig_business_account_id:
        logger.info("instagram_dms.entry.dropped reason=missing_account_id")
        return
    creator_id = _resolve_creator_from_ig_account(ig_business_account_id)
    if not creator_id:
        stats["dropped_no_creator"] += 1
        logger.info(
            "instagram_dms.dropped.no_creator ig_account_hint=%s",
            _id_hint(ig_business_account_id),
        )
        return
    messaging = entry.get("messaging") or []
    if not isinstance(messaging, list):
        logger.info(
            "instagram_dms.entry.dropped reason=messaging_not_list ig_account_hint=%s",
            _id_hint(ig_business_account_id),
        )
        return
    logger.info(
        "instagram_dms.entry.mapped creator=%s ig_account_hint=%s messages=%s",
        creator_id,
        _id_hint(ig_business_account_id),
        len(messaging),
    )
    for msg in messaging:
        persisted = _persist_message(creator_id, ig_business_account_id, msg)
        if persisted:
            stats["messages_ingested"] += 1
            if _create_manager_notification(creator_id, persisted):
                logger.info(
                    "instagram_dms.manager_notification.created user=%s mid_hint=%s",
                    creator_id,
                    _id_hint(str(persisted.get("ig_message_id") or "")),
                )
            else:
                logger.info(
                    "instagram_dms.manager_notification.skipped user=%s reason=not_manager_worthy direction=%s has_body=%s attachment_types=%s",
                    creator_id,
                    persisted.get("direction"),
                    bool(persisted.get("body")),
                    ",".join(_attachment_types(persisted.get("attachments"))),
                )


def _resolve_creator_from_ig_account(ig_account_id: str) -> str | None:
    """Look up the babyg creator whose oauth_connections row for
    provider='instagram' has provider_account_id matching this
    IG business account id."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("oauth_connections")
            .select("user_id")
            .eq("provider", "instagram")
            .eq("provider_account_id", ig_account_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "instagram_dms.resolve_creator.read_failed ig_account_hint=%s",
            _id_hint(ig_account_id),
        )
        return None
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        logger.info(
            "instagram_dms.resolve_creator.not_found ig_account_hint=%s",
            _id_hint(ig_account_id),
        )
        return None
    return str(rows[0].get("user_id") or "") or None


def _persist_message(
    creator_id: str, ig_business_account_id: str, msg: dict[str, Any]
) -> dict[str, Any] | None:
    """Upsert the thread + append the message.

    Returns the persisted message/thread context when the message was
    new, None on skip/failure. Checking for an existing message before
    bumping the thread keeps Meta webhook retries from inflating unread
    counts.
    """
    if not isinstance(msg, dict):
        logger.info("instagram_dms.message.dropped reason=not_object")
        return None
    message = msg.get("message")
    if not isinstance(message, dict):
        logger.info(
            "instagram_dms.message.dropped reason=unsupported_event keys=%s",
            ",".join(sorted(str(k)[:40] for k in msg)),
        )
        return None
    ig_message_id = str(message.get("mid") or "").strip()
    if not ig_message_id:
        logger.info(
            "instagram_dms.message.dropped reason=missing_mid message_keys=%s",
            ",".join(sorted(str(k)[:40] for k in message)),
        )
        return None
    sender_id = str((msg.get("sender") or {}).get("id") or "").strip()
    recipient_id = str((msg.get("recipient") or {}).get("id") or "").strip()
    if not sender_id or not recipient_id:
        logger.info(
            "instagram_dms.message.dropped reason=missing_sender_or_recipient mid_hint=%s",
            _id_hint(ig_message_id),
        )
        return None
    if _message_exists(creator_id=creator_id, ig_message_id=ig_message_id):
        logger.info(
            "instagram_dms.message.dropped reason=duplicate creator=%s mid_hint=%s",
            creator_id,
            _id_hint(ig_message_id),
        )
        return None

    is_echo = bool(message.get("is_echo"))
    if is_echo or sender_id == ig_business_account_id:
        direction = "outbound"
        peer_ig_id = recipient_id
        peer_username = str((msg.get("recipient") or {}).get("username") or "").strip()
    else:
        direction = "inbound"
        peer_ig_id = sender_id
        peer_username = str((msg.get("sender") or {}).get("username") or "").strip()

    body = str(message.get("text") or "")[:8000] or None
    attachments = message.get("attachments") or []
    if not isinstance(attachments, list):
        logger.info(
            "instagram_dms.message.attachments_ignored reason=not_list mid_hint=%s",
            _id_hint(ig_message_id),
        )
        attachments = []
    received_at = _timestamp_to_iso(msg.get("timestamp"))

    # We use ig_thread_id = the peer id per (creator, peer) —
    # Meta's messaging API is 1:1 per conversation, so peer id
    # uniquely identifies the thread.
    thread_row = _upsert_thread(
        creator_id=creator_id,
        ig_thread_id=peer_ig_id,
        ig_peer_user_id=peer_ig_id,
        peer_username=peer_username or None,
        last_message_at=received_at,
        inbound=(direction == "inbound"),
    )
    if not thread_row:
        return None
    thread_uuid = str(thread_row.get("id") or "")
    if not thread_uuid:
        return None

    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_messages")
            .upsert(
                {
                    "thread_id": thread_uuid,
                    "creator_id": creator_id,
                    "ig_message_id": ig_message_id,
                    "direction": direction,
                    "sender_ig_id": sender_id,
                    "body": body,
                    "attachments": attachments,
                    "received_at": received_at,
                },
                on_conflict="creator_id,ig_message_id",
            )
            .execute()
        )
    except Exception:
        logger.exception(
            "instagram_dms.persist_message.write_failed mid_hint=%s",
            _id_hint(ig_message_id),
        )
        return None
    rows = list(getattr(result, "data", None) or [])
    row = rows[0] if rows else {}
    logger.info(
        "instagram_dms.persist_message.ok creator=%s direction=%s mid_hint=%s has_body=%s attachment_types=%s",
        creator_id,
        direction,
        _id_hint(ig_message_id),
        bool(body),
        ",".join(_attachment_types(attachments)),
    )
    return {
        "id": row.get("id"),
        "thread_id": thread_uuid,
        "thread_created": bool(thread_row.get("_created")),
        "ig_message_id": ig_message_id,
        "direction": direction,
        "peer_ig_id": peer_ig_id,
        "peer_username": peer_username or None,
        "body": body,
        "attachments": attachments,
        "received_at": received_at,
    }


def _message_exists(*, creator_id: str, ig_message_id: str) -> bool:
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_messages")
            .select("id")
            .eq("creator_id", creator_id)
            .eq("ig_message_id", ig_message_id)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "instagram_dms.message_exists.failed creator=%s mid_hint=%s",
            creator_id,
            _id_hint(ig_message_id),
        )
        return False
    return bool(getattr(result, "data", None))


def _create_manager_notification(
    creator_id: str, message: dict[str, Any]
) -> bool:
    if message.get("direction") != "inbound":
        logger.info(
            "instagram_dms.manager_notification.skipped user=%s reason=not_inbound",
            creator_id,
        )
        return False
    body = str(message.get("body") or "")
    attachments = message.get("attachments") if isinstance(message, dict) else []
    attachment_types = _attachment_types(attachments)
    has_manager_attachment = bool(attachment_types)
    if not dm_briefs.needs_brief(body) and not has_manager_attachment:
        logger.info(
            "instagram_dms.manager_notification.skipped user=%s reason=low_importance has_body=%s attachment_types=%s",
            creator_id,
            bool(body),
            ",".join(attachment_types),
        )
        return False
    thread_id = str(message.get("thread_id") or "")
    message_id = str(message.get("id") or "")
    ig_message_id = str(message.get("ig_message_id") or "")
    if not thread_id or not ig_message_id:
        logger.info(
            "instagram_dms.manager_notification.skipped user=%s reason=missing_thread_or_mid",
            creator_id,
        )
        return False
    peer = str(message.get("peer_username") or message.get("peer_ig_id") or "").strip()
    peer_label = f"@{peer}" if peer and not peer.startswith("@") else peer or "someone"
    link_path = f"/creator/instagram/dms?thread={thread_id}#ig-thread-{thread_id}"
    attachment_label = _attachment_label(attachment_types)
    priority = (
        "high"
        if _looks_like_collab_or_deal(body) or attachment_label in {"reel", "post"}
        else "normal"
    )
    title_subject = (
        f"instagram {attachment_label}"
        if attachment_label
        else "instagram message"
    )
    return notifications.create(
        user_id=creator_id,
        kind="new_dm",
        title=f"new {title_subject} from {peer_label}",
        body=_manager_body(body, attachment_label=attachment_label),
        link_path=link_path,
        priority=priority,
        source_provider="instagram",
        source_event_id=f"instagram:message:{ig_message_id}",
        source_thread_id=thread_id,
        underlying_type="instagram_dm_message",
        underlying_id=message_id or ig_message_id,
        metadata={
            "ig_thread_id": thread_id,
            "ig_message_id": ig_message_id,
            "peer_ig_id": message.get("peer_ig_id"),
            "peer_username": message.get("peer_username"),
            "attachment_types": attachment_types,
            "suggested_action": "draft_reply",
        },
    )


def _looks_like_collab_or_deal(body: str) -> bool:
    norm = (body or "").lower()
    return any(
        token in norm
        for token in (
            "collab",
            "collaboration",
            "brand deal",
            "sponsor",
            "sponsorship",
            "paid",
            "rate",
            "rates",
            "usage rights",
            "deadline",
        )
    )


def _manager_body(body: str, *, attachment_label: str | None = None) -> str:
    clean = " ".join((body or "").split())
    if not clean:
        if attachment_label:
            return (
                f"They sent you an Instagram {attachment_label}. "
                "I can draft a reply."
            )
        return "This Instagram DM may need a response. I can draft a reply."
    preview = clean[:180]
    if len(clean) > 180:
        preview = preview.rstrip() + "..."
    return f"{preview} I can draft a reply."


def _attachment_types(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("type") or "").strip().lower()
        if not raw or raw not in _MANAGER_ATTACHMENT_TYPES or raw in seen:
            continue
        seen.add(raw)
        out.append(raw)
    return out


def _attachment_label(types: list[str]) -> str | None:
    if not types:
        return None
    if "reel" in types or "ig_reel" in types:
        return "reel"
    if "post" in types or "ig_post" in types:
        return "post"
    if "story_mention" in types:
        return "story"
    if "image" in types or "video" in types:
        return "media"
    return "attachment"


def _id_hint(value: str) -> str:
    raw = str(value or "").strip()
    if len(raw) <= 8:
        return f"len:{len(raw)}"
    return f"{raw[:4]}...{raw[-4:]}"


def _upsert_thread(
    *,
    creator_id: str,
    ig_thread_id: str,
    ig_peer_user_id: str,
    peer_username: str | None = None,
    last_message_at: str,
    inbound: bool,
) -> dict[str, Any] | None:
    """Return the thread row (existing or freshly-inserted).

    We can't rely on the returned data of `upsert` to give us the
    row id when the row already existed — supabase's PostgREST-based
    upsert can be quiet on conflict. So we do a select-then-upsert
    dance: if the row exists, we PATCH last_message_at + bump
    unread_count for inbound. If not, we insert.
    """
    client = supabase_client.get_service_client()
    try:
        existing = (
            client.table("instagram_dm_threads")
            .select("id,unread_count")
            .eq("creator_id", creator_id)
            .eq("ig_thread_id", ig_thread_id)
            .limit(1)
            .execute()
        )
        rows = list(getattr(existing, "data", None) or [])
        if rows:
            row = rows[0]
            update_payload: dict[str, Any] = {"last_message_at": last_message_at}
            if peer_username:
                update_payload["peer_username"] = peer_username[:255]
            if inbound:
                update_payload["unread_count"] = int(row.get("unread_count") or 0) + 1
            client.table("instagram_dm_threads").update(update_payload).eq(
                "id", row["id"]
            ).execute()
            return {"id": row["id"], "_created": False}
        insert_payload = {
            "creator_id": creator_id,
            "ig_thread_id": ig_thread_id,
            "ig_peer_user_id": ig_peer_user_id,
            "peer_username": peer_username[:255] if peer_username else None,
            "last_message_at": last_message_at,
            "unread_count": 1 if inbound else 0,
        }
        result = (
            client.table("instagram_dm_threads")
            .insert(insert_payload)
            .execute()
        )
        rows = list(getattr(result, "data", None) or [])
        if not rows:
            return None
        return {**rows[0], "_created": True}
    except Exception:
        logger.exception(
            "instagram_dms.upsert_thread.failed creator=%s thread=%s",
            creator_id,
            ig_thread_id,
        )
        return None


def mark_thread_read_for_creator(*, user_id: str, thread_id: str) -> bool:
    """Clear the unread counter for one owner-scoped Instagram DM thread."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_threads")
            .update({"unread_count": 0})
            .eq("creator_id", user_id)
            .eq("id", thread_id)
            .execute()
        )
    except Exception:
        logger.exception(
            "instagram_dms.mark_thread_read_for_creator.failed user=%s thread=%s",
            user_id,
            thread_id,
        )
        return False
    return bool(getattr(result, "data", None))


def _timestamp_to_iso(value: Any) -> str:
    """Meta uses millisecond epoch. Coerce to ISO-8601 UTC.

    Bad or missing value -> now(). We don't want the row to fail
    to insert because Meta forgot the timestamp on one edge-case
    payload."""
    try:
        millis = int(value)
    except (TypeError, ValueError):
        return datetime.now(UTC).isoformat()
    if millis <= 0:
        return datetime.now(UTC).isoformat()
    return datetime.fromtimestamp(millis / 1000.0, tz=UTC).isoformat()
