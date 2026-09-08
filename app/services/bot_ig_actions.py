"""IG DM action executor for approved proposals.

Extracted from app/services/bot.py to keep that module from growing
without bound. Anything to do with the confirm/cancel flow of an
action_proposals row whose action_type is `instagram.send_dm` lives
here.

Public surface:
  - is_instagram_send_dm_action(action_type) -> bool
  - confirm_instagram_send_dm_action(...)    -> BotActionResult
  - cancel_instagram_send_dm_action(...)     -> BotActionResult

Wired from bot.confirm_action / bot.cancel_action. Callers pass in
the resolved tool_calls dict + message id; this module owns the
lifecycle from there.

The Meta Send API call happens inside _execute; it never raises,
always returns a structured {ok, reason?, detail?, message_id?}
dict the confirm wrapper turns into a user-facing chat message.
Meta's 24-hour messaging window is enforced by the Send API layer
and surfaced here as reason='outside_messaging_window'.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from app.core import supabase_client
from app.integrations import instagram_meta
from app.services import action_proposals, oauth_connections

logger = logging.getLogger(__name__)


# We take these two callables from the bot module so this file has
# no circular import back to bot.py. bot.py wires them at
# call-time.
UpdateToolCallsFn = Callable[..., bool]
CreateMessageFn = Callable[..., str | None]


@dataclass
class IgActionResult:
    """Mirror of bot.BotActionResult's shape, kept small on purpose.

    bot.confirm_action wraps this into a real BotActionResult before
    returning to the route — we keep the shape identical so the
    conversion is a plain constructor call.
    """
    message: str
    executed: bool = False
    action_type: str | None = None
    record_id: str | None = None


def is_instagram_send_dm_action(action_type: str) -> bool:
    return action_type == "instagram.send_dm"


def confirm_instagram_send_dm_action(
    *,
    user_id: str,
    message_id: str,
    tool_calls: dict[str, Any],
    action_type: str,
    update_message_tool_calls: UpdateToolCallsFn,
    create_message: CreateMessageFn,
) -> IgActionResult:
    """User-approved IG DM send. Uses Meta's Send API.

    Mirrors bot._confirm_gmail_send_action's lifecycle:
      1. lock the proposal (confirm -> executing)
      2. resolve peer id from the babyg thread row (never trust the
         payload alone — the payload carries the babyg thread uuid,
         not the peer ig id)
      3. call instagram_meta.send_direct_message
      4. mark_executed / mark_failed and rewrite the tool_calls status

    Meta's 24-hour messaging window is enforced by the Send API layer
    (InstagramMessageWindowError surfaces as reason=
    'outside_messaging_window', which maps to a specific user-facing
    message: "the window closed, ask them to DM you first").
    """
    proposal_id = str(tool_calls.get("proposal_id") or "")
    if not proposal_id:
        message = "That instagram dm proposal is missing its approval record."
        create_message(user_id=user_id, role="assistant", content=message)
        return IgActionResult(message=message, action_type=action_type)

    if not action_proposals.confirm_proposal(
        proposal_id=proposal_id, user_id=user_id
    ):
        refreshed = action_proposals.get_for_user(
            proposal_id=proposal_id, user_id=user_id
        )
        status = str((refreshed or {}).get("status") or "handled")
        updated = {
            **tool_calls,
            "status": status,
            "result": {
                "ok": False,
                "record_id": None,
                "error": (refreshed or {}).get("error_code"),
            },
        }
        update_message_tool_calls(
            message_id=message_id, user_id=user_id, tool_calls=updated
        )
        message = (
            "That instagram dm could not be approved. Reconnect Instagram "
            "with messaging access, then try again."
        )
        create_message(user_id=user_id, role="assistant", content=message)
        return IgActionResult(message=message, action_type=action_type)

    if not action_proposals.mark_executing(
        proposal_id=proposal_id, user_id=user_id
    ):
        message = "That instagram dm was already handled."
        create_message(user_id=user_id, role="assistant", content=message)
        return IgActionResult(message=message, action_type=action_type)

    executing = {
        **tool_calls,
        "status": "executing",
        "result": {"ok": None, "record_id": None},
    }
    update_message_tool_calls(
        message_id=message_id, user_id=user_id, tool_calls=executing
    )

    payload = tool_calls.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    result = _execute_send(user_id=user_id, payload=payload)
    if result.get("ok"):
        meta_message_id = str(result.get("message_id") or "")
        action_proposals.mark_executed(
            proposal_id=proposal_id,
            user_id=user_id,
            external_result_id=meta_message_id or None,
        )
        final_status = "executed"
        message = "sent your instagram dm reply."
        ok = True
    else:
        error_code = str(result.get("reason") or "instagram_send_failed")
        error_message = str(result.get("detail") or "Meta Send API refused.")
        action_proposals.mark_failed(
            proposal_id=proposal_id,
            user_id=user_id,
            error_code=error_code,
            error_message=error_message,
        )
        final_status = "failed"
        meta_message_id = ""
        if error_code == "outside_messaging_window":
            message = (
                "instagram closed the 24-hour reply window on this thread. "
                "ask them to dm you again and try later."
            )
        elif error_code == "no_ig_token":
            message = (
                "instagram lost access. reconnect it in settings and try again."
            )
        else:
            message = "i couldn't send that instagram dm. nothing was sent."
        ok = False

    result_tool_calls = {
        **tool_calls,
        "status": final_status,
        "result": {"ok": ok, "record_id": meta_message_id or None},
    }
    update_message_tool_calls(
        message_id=message_id, user_id=user_id, tool_calls=result_tool_calls
    )
    create_message(user_id=user_id, role="assistant", content=message)
    return IgActionResult(
        message=message,
        executed=ok,
        action_type=action_type,
        record_id=meta_message_id or None,
    )


def cancel_instagram_send_dm_action(
    *,
    user_id: str,
    message_id: str,
    tool_calls: dict[str, Any],
    action_type: str,
    update_message_tool_calls: UpdateToolCallsFn,
    create_message: CreateMessageFn,
) -> IgActionResult:
    """User cancelled a pending IG DM proposal. Cancels the row +
    flips the tool_calls status; no Send API call happens."""
    proposal_id = str(tool_calls.get("proposal_id") or "")
    if proposal_id:
        action_proposals.cancel_proposal(
            proposal_id=proposal_id, user_id=user_id
        )
    cancelled = {
        **tool_calls,
        "status": "cancelled",
        "result": {"ok": False, "record_id": None},
    }
    update_message_tool_calls(
        message_id=message_id,
        user_id=user_id,
        tool_calls=cancelled,
        expected_status="pending",
    )
    message = "cancelled. no instagram dm was sent."
    create_message(user_id=user_id, role="assistant", content=message)
    return IgActionResult(message=message, action_type=action_type)


def _execute_send(
    *, user_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Do the actual Send API call. Returns a structured result the
    confirm wrapper turns into the correct proposal outcome. Never
    raises — every failure comes back as {ok: False, reason: ...}."""
    babyg_thread_id = str(payload.get("ig_thread_id") or "").strip()
    body = str(payload.get("body") or "").strip()
    if not babyg_thread_id or not body:
        return {"ok": False, "reason": "missing_thread_or_body"}
    thread_row = _load_ig_thread(
        user_id=user_id, thread_uuid=babyg_thread_id
    )
    if not thread_row:
        return {"ok": False, "reason": "thread_not_found"}
    peer_ig_id = str(thread_row.get("ig_peer_user_id") or "").strip()
    if not peer_ig_id:
        return {"ok": False, "reason": "no_peer_ig_id"}
    ig_connection = oauth_connections.get_instagram_connection(user_id) or {}
    ig_account_id = oauth_connections.instagram_account_id(ig_connection) or ""
    if not ig_account_id:
        return {"ok": False, "reason": "no_ig_connection"}
    try:
        token = oauth_connections.access_token_for_instagram(user_id)
    except Exception:
        logger.exception("bot_ig_actions.execute.token user=%s", user_id)
        return {"ok": False, "reason": "token_lookup_failed"}
    if not token:
        return {"ok": False, "reason": "no_ig_token"}
    try:
        meta_message_id = instagram_meta.send_direct_message(
            token,
            ig_business_account_id=ig_account_id,
            recipient_ig_user_id=peer_ig_id,
            body=body,
        )
    except instagram_meta.InstagramMessageWindowError as exc:
        return {
            "ok": False,
            "reason": "outside_messaging_window",
            "detail": str(exc)[:200],
        }
    except instagram_meta.InstagramError as exc:
        logger.warning(
            "bot_ig_actions.execute.send_failed user=%s error=%s",
            user_id,
            exc,
        )
        return {
            "ok": False,
            "reason": "instagram_send_failed",
            "detail": str(exc)[:200],
        }
    except Exception:
        logger.exception("bot_ig_actions.execute.crashed user=%s", user_id)
        return {"ok": False, "reason": "instagram_send_failed"}
    return {"ok": True, "message_id": meta_message_id}


def _load_ig_thread(
    *, user_id: str, thread_uuid: str
) -> dict[str, Any] | None:
    """Owner-scoped read of one instagram_dm_threads row. A thread the
    creator doesn't own is invisible — extra check on top of RLS."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("instagram_dm_threads")
            .select("id,ig_thread_id,ig_peer_user_id")
            .eq("creator_id", user_id)
            .eq("id", thread_uuid)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception(
            "bot_ig_actions.load_thread.failed user=%s thread=%s",
            user_id,
            thread_uuid,
        )
        return None
    rows = list(getattr(result, "data", None) or [])
    return rows[0] if rows else None
