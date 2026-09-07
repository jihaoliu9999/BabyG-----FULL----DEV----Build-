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

logger = logging.getLogger(__name__)


def ingest_webhook_payload(payload: dict[str, Any]) -> dict[str, int]:
    """Top-level entrypoint. Returns a small stats dict for the
    webhook route's log line — never raises."""
    stats = {"entries": 0, "messages_ingested": 0, "dropped_no_creator": 0, "errors": 0}
    if not isinstance(payload, dict):
        return stats
    if payload.get("object") != "instagram":
        # We only handle Instagram right now. Meta uses the same
        # webhook shape for Facebook Pages / WhatsApp; ignore quietly.
        return stats
    entries = payload.get("entry") or []
    if not isinstance(entries, list):
        return stats
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
        return
    creator_id = _resolve_creator_from_ig_account(ig_business_account_id)
    if not creator_id:
        stats["dropped_no_creator"] += 1
        logger.info(
            "instagram_dms.dropped.no_creator ig_account=%s",
            ig_business_account_id,
        )
        return
    for msg in entry.get("messaging") or []:
        if _persist_message(creator_id, ig_business_account_id, msg):
            stats["messages_ingested"] += 1


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
            "instagram_dms.resolve_creator.read_failed ig_account=%s",
            ig_account_id,
        )
        return None
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return None
    return str(rows[0].get("user_id") or "") or None


def _persist_message(
    creator_id: str, ig_business_account_id: str, msg: dict[str, Any]
) -> bool:
    """Upsert the thread + append the message. Returns True on a
    successful insert (message was new), False on skip/failure."""
    if not isinstance(msg, dict):
        return False
    message = msg.get("message") or {}
    ig_message_id = str(message.get("mid") or "").strip()
    if not ig_message_id:
        return False
    sender_id = str((msg.get("sender") or {}).get("id") or "").strip()
    recipient_id = str((msg.get("recipient") or {}).get("id") or "").strip()
    if not sender_id or not recipient_id:
        return False

    is_echo = bool(message.get("is_echo"))
    if is_echo or sender_id == ig_business_account_id:
        direction = "outbound"
        peer_ig_id = recipient_id
    else:
        direction = "inbound"
        peer_ig_id = sender_id

    body = str(message.get("text") or "")[:8000] or None
    attachments = message.get("attachments") or []
    if not isinstance(attachments, list):
        attachments = []
    received_at = _timestamp_to_iso(msg.get("timestamp"))

    # We use ig_thread_id = the peer id per (creator, peer) —
    # Meta's messaging API is 1:1 per conversation, so peer id
    # uniquely identifies the thread.
    thread_row = _upsert_thread(
        creator_id=creator_id,
        ig_thread_id=peer_ig_id,
        ig_peer_user_id=peer_ig_id,
        last_message_at=received_at,
        inbound=(direction == "inbound"),
    )
    if not thread_row:
        return False
    thread_uuid = str(thread_row.get("id") or "")
    if not thread_uuid:
        return False

    try:
        (
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
            "instagram_dms.persist_message.write_failed mid=%s", ig_message_id
        )
        return False
    return True


def _upsert_thread(
    *,
    creator_id: str,
    ig_thread_id: str,
    ig_peer_user_id: str,
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
            if inbound:
                update_payload["unread_count"] = int(row.get("unread_count") or 0) + 1
            client.table("instagram_dm_threads").update(update_payload).eq(
                "id", row["id"]
            ).execute()
            return {"id": row["id"]}
        insert_payload = {
            "creator_id": creator_id,
            "ig_thread_id": ig_thread_id,
            "ig_peer_user_id": ig_peer_user_id,
            "last_message_at": last_message_at,
            "unread_count": 1 if inbound else 0,
        }
        result = (
            client.table("instagram_dm_threads")
            .insert(insert_payload)
            .execute()
        )
        rows = list(getattr(result, "data", None) or [])
        return rows[0] if rows else None
    except Exception:
        logger.exception(
            "instagram_dms.upsert_thread.failed creator=%s thread=%s",
            creator_id,
            ig_thread_id,
        )
        return None


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
