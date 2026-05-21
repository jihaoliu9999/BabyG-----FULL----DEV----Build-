"""babyg assistant orchestration and message persistence."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from postgrest.exceptions import APIError as PostgrestAPIError

from app.agent.tools import read_only
from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.integrations import anthropic_client
from app.services import bookings, jobs, prompts, reminders

logger = logging.getLogger(__name__)

MessageRole = Literal["user", "assistant", "system"]
ActionType = Literal[
    "create_booking",
    "create_content_reminder",
    "submit_creator_listing",
]
DraftKind = Literal[
    "caption",
    "brand_reply",
    "creator_dm",
    "content_plan",
    "negotiation",
    "general",
]

MAX_USER_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20
ALLOWED_ACTION_TYPES = (
    "create_booking",
    "create_content_reminder",
    "submit_creator_listing",
)
ACTION_DATETIME_RE = re.compile(
    r"\b(\d{4}-\d{2}-\d{2}(?:[T ][0-2]\d:[0-5]\d(?::[0-5]\d)?(?:Z)?)?)\b"
)
TITLE_RE = re.compile(r"\b(?:called|titled|named)\s+(.+?)(?:\s+(?:on|at|for)\b|$)", re.I)

OUT_OF_SCOPE_KEYWORDS = (
    "debug my code",
    "write code",
    "fix my code",
    "leetcode",
    "homework",
    "essay for school",
    "medical advice",
    "legal advice",
    "therapy",
    "diagnose",
    "dating advice",
    "buy followers",
    "bot followers",
    "fake engagement",
    "algorithm manipulation",
)


@dataclass(frozen=True)
class BotTurnResult:
    response: str
    limited: bool = False
    flagged: bool = False
    flag_category: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass(frozen=True)
class BotActionResult:
    message: str
    found: bool = True
    executed: bool = False
    action_type: str | None = None
    record_id: str | None = None


def list_messages(user_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
    """Return oldest-first chat history for one creator."""
    try:
        result = (
            supabase_client.get_service_client()
            .table("bot_messages")
            .select("*")
            .eq("user_id", user_id)
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("bot message list failed for %s", user_id)
        return []
    rows = getattr(result, "data", None) or []
    return list(reversed(rows))


def handle_creator_message(*, user_id: str, content: str) -> BotTurnResult:
    """Persist a creator message and return babyg's response."""
    user_content = (content or "").strip()[:MAX_USER_MESSAGE_CHARS]
    if not user_content:
        return BotTurnResult(response="Send me a creator task and I'll help.")

    scope_flag = _scope_flag(user_content)
    create_message(
        user_id=user_id,
        role="user",
        content=user_content,
        flagged=scope_flag is not None,
        flag_category=scope_flag,
    )

    if scope_flag is not None:
        response_text = prompts.BABYG_SCOPE_REFUSAL
        create_message(
            user_id=user_id,
            role="assistant",
            content=response_text,
            flagged=True,
            flag_category=scope_flag,
        )
        return BotTurnResult(
            response=response_text,
            flagged=True,
            flag_category=scope_flag,
        )

    proposal = _build_action_proposal(user_content)
    if proposal is not None:
        response_text = proposal["preview"]
        create_message(
            user_id=user_id,
            role="assistant",
            content=response_text,
            tool_calls=proposal,
        )
        return BotTurnResult(response=response_text)

    context = build_context(user_id)
    history = _messages_for_claude(list_messages(user_id, limit=MAX_HISTORY_MESSAGES))
    system_prompt = prompts.babyg_system_prompt(
        context,
        draft_kind=_draft_kind(user_content),
    )

    try:
        claude_response = anthropic_client.complete_chat(
            system_prompt=system_prompt,
            messages=history,
        )
        response_text = (
            claude_response.text
            or "I drafted a response, but it came back empty. Try me again?"
        )
        input_tokens = claude_response.input_tokens
        output_tokens = claude_response.output_tokens
    except anthropic_client.ClaudeNotConfiguredError:
        response_text = (
            "babyg chat is wired up, but Claude is not configured yet. "
            "Add ANTHROPIC_API_KEY on the server and I can start drafting."
        )
        input_tokens = 0
        output_tokens = 0
    except anthropic_client.ClaudeCallError:
        response_text = (
            "I couldn't reach Claude for that turn. Your message is saved; "
            "try again in a minute."
        )
        input_tokens = 0
        output_tokens = 0

    create_message(user_id=user_id, role="assistant", content=response_text)
    return BotTurnResult(
        response=response_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


def create_message(
    *,
    user_id: str,
    role: MessageRole,
    content: str,
    flagged: bool = False,
    flag_category: str | None = None,
    tool_calls: Any | None = None,
) -> str | None:
    body = {
        "user_id": user_id,
        "role": role,
        "content": content,
        "tool_calls": tool_calls,
        "flagged": flagged,
        "flag_category": flag_category,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("bot_messages")
            .insert(body)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("bot message insert failed for %s", user_id)
        return None
    rows = getattr(result, "data", None) or []
    return str(rows[0]["id"]) if rows else None


def confirm_action(*, user_id: str, message_id: str) -> BotActionResult:
    row = _get_message_for_user(message_id=message_id, user_id=user_id)
    if row is None:
        return BotActionResult(message="I couldn't find that action.", found=False)
    tool_calls = _proposal_from_row(row)
    if tool_calls is None:
        return BotActionResult(message="That message is not a pending action.")

    status = tool_calls.get("status")
    action_type = str(tool_calls.get("action_type") or "")
    if status != "pending":
        return BotActionResult(
            message=f"That action is already {status}.",
            action_type=action_type or None,
        )
    if action_type not in ALLOWED_ACTION_TYPES:
        return BotActionResult(message="That action is not allowed.")

    locked = {
        **tool_calls,
        "status": "confirmed",
        "result": {"ok": None, "record_id": None},
    }
    if not _update_message_tool_calls(
        message_id=message_id,
        user_id=user_id,
        tool_calls=locked,
        expected_status="pending",
    ):
        return BotActionResult(
            message="That action was already handled.",
            action_type=action_type,
        )

    payload = tool_calls.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    record_id = _execute_confirmed_action(
        action_type=action_type,
        user_id=user_id,
        payload=payload,
    )
    ok = record_id is not None
    result_tool_calls = {
        **locked,
        "result": {"ok": ok, "record_id": record_id},
    }
    _update_message_tool_calls(
        message_id=message_id,
        user_id=user_id,
        tool_calls=result_tool_calls,
    )

    if record_id is not None:
        message = _success_message(action_type, record_id)
    else:
        message = "I couldn't save that local action. Nothing external happened."
    create_message(user_id=user_id, role="assistant", content=message)
    return BotActionResult(
        message=message,
        executed=ok,
        action_type=action_type,
        record_id=record_id,
    )


def cancel_action(*, user_id: str, message_id: str) -> BotActionResult:
    row = _get_message_for_user(message_id=message_id, user_id=user_id)
    if row is None:
        return BotActionResult(message="I couldn't find that action.", found=False)
    tool_calls = _proposal_from_row(row)
    if tool_calls is None:
        return BotActionResult(message="That message is not a pending action.")
    status = tool_calls.get("status")
    action_type = str(tool_calls.get("action_type") or "")
    if status != "pending":
        return BotActionResult(
            message=f"That action is already {status}.",
            action_type=action_type or None,
        )
    cancelled = {
        **tool_calls,
        "status": "cancelled",
        "result": {"ok": False, "record_id": None},
    }
    _update_message_tool_calls(
        message_id=message_id,
        user_id=user_id,
        tool_calls=cancelled,
        expected_status="pending",
    )
    message = "Cancelled. Nothing was saved."
    create_message(user_id=user_id, role="assistant", content=message)
    return BotActionResult(message=message, action_type=action_type)


def build_context(user_id: str) -> dict[str, Any]:
    return read_only.collect_context(user_id)


def _get_message_for_user(*, message_id: str, user_id: str) -> dict[str, Any] | None:
    mid = safe_uuid(message_id)
    if not mid:
        return None
    try:
        result = (
            supabase_client.get_service_client()
            .table("bot_messages")
            .select("*")
            .eq("id", mid)
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
    except PostgrestAPIError:
        logger.exception("bot action lookup failed: %s", message_id)
        return None
    rows = getattr(result, "data", None) or []
    return rows[0] if rows else None


def _update_message_tool_calls(
    *,
    message_id: str,
    user_id: str,
    tool_calls: dict[str, Any],
    expected_status: str | None = None,
) -> bool:
    mid = safe_uuid(message_id)
    if not mid:
        return False
    try:
        query = (
            supabase_client.get_service_client()
            .table("bot_messages")
            .update({"tool_calls": tool_calls})
            .eq("id", mid)
            .eq("user_id", user_id)
        )
        if expected_status is not None:
            query = query.eq("tool_calls->>status", expected_status)
        result = query.execute()
    except PostgrestAPIError:
        logger.exception("bot action update failed: %s", message_id)
        return False
    return bool(getattr(result, "data", None))


def _proposal_from_row(row: dict[str, Any]) -> dict[str, Any] | None:
    tool_calls = row.get("tool_calls")
    if not isinstance(tool_calls, dict):
        return None
    if tool_calls.get("kind") != "proposed_action":
        return None
    return tool_calls


def _messages_for_claude(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        messages.append({"role": role, "content": content})
    return messages


def _build_action_proposal(content: str) -> dict[str, Any] | None:
    action_type = _action_type(content)
    if action_type is None:
        return None
    payload = _action_payload(action_type=action_type, content=content)
    preview = _action_preview(action_type=action_type, payload=payload)
    return {
        "kind": "proposed_action",
        "status": "pending",
        "action_type": action_type,
        "payload": payload,
        "preview": preview,
        "result": None,
    }


def _action_type(content: str) -> ActionType | None:
    lowered = content.lower()
    if not any(word in lowered for word in ("create", "add", "make", "post", "submit")):
        return None
    if "listing" in lowered or "opportunity" in lowered or "job" in lowered:
        return "submit_creator_listing"
    if "remind" in lowered or "reminder" in lowered:
        return "create_content_reminder"
    if any(word in lowered for word in ("booking", "calendar", "event", "deadline")):
        return "create_booking"
    return None


def _action_payload(*, action_type: str, content: str) -> dict[str, Any]:
    if action_type == "create_booking":
        return _booking_payload(content)
    if action_type == "create_content_reminder":
        return _reminder_payload(content)
    if action_type == "submit_creator_listing":
        return _listing_payload(content)
    return {}


def _booking_payload(content: str) -> dict[str, Any]:
    lowered = content.lower()
    btype = "event"
    for candidate in bookings.TYPES:
        if candidate == "restaurant":
            continue
        if candidate in lowered:
            btype = candidate
            break
    return {
        "title": _title_from_content(content, fallback="Creator event"),
        "type": btype,
        "starts_at": _datetime_from_content(content),
        "ends_at": None,
        "notes": _notes_from_content(content),
        "venue_name": None,
        "status": "confirmed",
    }


def _reminder_payload(content: str) -> dict[str, Any]:
    title = _title_from_content(content, fallback="Content reminder")
    return {
        "kind": "content_reminder",
        "payload": {"title": title, "source": "babyg"},
        "fire_at": _datetime_from_content(content),
    }


def _listing_payload(content: str) -> dict[str, Any]:
    lowered = content.lower()
    listing_type = "collab"
    if "ugc" in lowered:
        listing_type = "ugc_gig"
    elif "hire" in lowered or "hiring" in lowered or "help" in lowered:
        listing_type = "hiring"
    elif "brand deal" in lowered or "brand opportunity" in lowered:
        listing_type = "brand_deal"
    title = _title_from_content(content, fallback="Creator opportunity")
    return {
        "title": title,
        "description": _notes_from_content(content) or content[:4000],
        "listing_type": listing_type,
        "compensation_text": None,
        "target_niches": [],
        "deadline": _datetime_from_content(content, required=False),
        "is_active": True,
    }


def _action_preview(*, action_type: str, payload: dict[str, Any]) -> str:
    if action_type == "create_booking":
        return (
            "I can create this local calendar item after you confirm:\n\n"
            f"Title: {payload['title']}\n"
            f"Type: {payload['type']}\n"
            f"Starts: {payload['starts_at']}\n\n"
            "Nothing has been saved yet."
        )
    if action_type == "create_content_reminder":
        title = (payload.get("payload") or {}).get("title")
        return (
            "I can create this local content reminder after you confirm:\n\n"
            f"Reminder: {title}\n"
            f"Fire at: {payload['fire_at']}\n\n"
            "Nothing has been saved yet."
        )
    return (
        "I can submit this creator listing after you confirm:\n\n"
        f"Title: {payload['title']}\n"
        f"Type: {payload['listing_type']}\n\n"
        "Nothing has been saved yet."
    )


def _execute_confirmed_action(
    *, action_type: str, user_id: str, payload: dict[str, Any]
) -> str | None:
    if action_type == "create_booking":
        return bookings.create(user_id=user_id, payload=payload)
    if action_type == "create_content_reminder":
        return reminders.create(user_id=user_id, payload=payload)
    if action_type == "submit_creator_listing":
        return jobs.create(poster_id=user_id, payload=payload)
    return None


def _success_message(action_type: str, record_id: str) -> str:
    if action_type == "create_booking":
        return f"Done. I created that local calendar item in babyg. Record: {record_id}"
    if action_type == "create_content_reminder":
        return f"Done. I created that local content reminder in babyg. Record: {record_id}"
    if action_type == "submit_creator_listing":
        return f"Done. I submitted that creator listing in babyg. Record: {record_id}"
    return f"Done. I saved that local action in babyg. Record: {record_id}"


def _datetime_from_content(content: str, *, required: bool = True) -> str | None:
    match = ACTION_DATETIME_RE.search(content)
    if match:
        return match.group(1).replace(" ", "T")
    if required:
        return "2099-01-01T09:00:00Z"
    return None


def _title_from_content(content: str, *, fallback: str) -> str:
    match = TITLE_RE.search(content)
    if not match:
        return fallback
    title = match.group(1).strip(" .,:;\"'")
    return title[:140] or fallback


def _notes_from_content(content: str) -> str | None:
    text = content.strip()
    return text[:2000] if text else None


def _scope_flag(content: str) -> str | None:
    lowered = content.lower()
    if any(keyword in lowered for keyword in OUT_OF_SCOPE_KEYWORDS):
        return "scope"
    return None


def _draft_kind(content: str) -> DraftKind | None:
    lowered = content.lower()
    is_draft_request = any(
        marker in lowered
        for marker in (
            "draft",
            "write",
            "caption",
            "reply",
            "respond",
            "response",
            "dm",
            "message",
            "content plan",
            "weekly plan",
            "negotiate",
            "negotiation",
        )
    )
    if not is_draft_request:
        return None
    if any(marker in lowered for marker in ("caption", "captions", "hook")):
        return "caption"
    if any(
        marker in lowered
        for marker in ("brand email", "brand message", "brand reply", "offer")
    ):
        return "brand_reply"
    if any(marker in lowered for marker in ("negotiate", "negotiation", "rate")):
        return "negotiation"
    if any(marker in lowered for marker in ("dm", "creator message", "collab message")):
        return "creator_dm"
    if any(marker in lowered for marker in ("content plan", "weekly plan", "calendar")):
        return "content_plan"
    return "general"
