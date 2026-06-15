"""babyg assistant orchestration and message persistence."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

from postgrest.exceptions import APIError as PostgrestAPIError

from app.agent.tools import read_only
from app.core import supabase_client
from app.core.uuid_guard import safe_uuid
from app.integrations import anthropic_client, google_calendar, google_gmail, instagram_meta, tavily
from app.services import bookings, jobs, oauth_connections, prompts, reminders

logger = logging.getLogger(__name__)

MessageRole = Literal["user", "assistant", "system"]
ActionType = Literal[
    "create_booking",
    "create_content_reminder",
    "submit_creator_listing",
    "create_gmail_draft",
]
DraftKind = Literal[
    "caption",
    "brand_reply",
    "creator_dm",
    "content_plan",
    "negotiation",
    "general",
]
TaskKind = Literal[
    "hot_drops",
    "planning",
    "offer_review",
    "networking",
    "calendar",
    "stats",
]

MAX_USER_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20
MAX_TOOL_ITERATIONS = 4
# Per-creator daily cap on web_search invocations. Process-local —
# resets on deploy, which is fine for an MVP cost-guard. A future
# Redis-backed limiter (see app/core/rate_limit.py for the pattern)
# can replace this without touching the tool surface.
WEB_SEARCH_DAILY_CAP = 20
_web_search_counter: dict[tuple[str, str], int] = {}


def _today_key() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y-%m-%d")


def _bump_web_search_counter(user_id: str) -> int:
    key = (str(user_id), _today_key())
    count = _web_search_counter.get(key, 0) + 1
    _web_search_counter[key] = count
    return count


# Parallel cap for the Instagram stats tool. Same process-local
# counter pattern as web_search above. Conservative 30/day per
# creator — Meta API insights are free per call, so the constraint
# is rate-limiting babyg's traffic against Meta's per-app budget,
# not cost.
INSTAGRAM_STATS_DAILY_CAP = 30
_instagram_stats_counter: dict[tuple[str, str], int] = {}


def _bump_instagram_stats_counter(user_id: str) -> int:
    key = (str(user_id), _today_key())
    count = _instagram_stats_counter.get(key, 0) + 1
    _instagram_stats_counter[key] = count
    return count


# Gmail inbox reads. Higher cap than IG stats because creators check
# email context far more often than per-post engagement — but still
# bounded so a runaway agent loop can't drain the daily Gmail quota.
GMAIL_INBOX_DAILY_CAP = 50
_gmail_inbox_counter: dict[tuple[str, str], int] = {}


def _bump_gmail_inbox_counter(user_id: str) -> int:
    key = (str(user_id), _today_key())
    count = _gmail_inbox_counter.get(key, 0) + 1
    _gmail_inbox_counter[key] = count
    return count


ALLOWED_ACTION_TYPES = (
    "create_booking",
    "create_content_reminder",
    "submit_creator_listing",
    "create_gmail_draft",
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

    proposal = None if _should_use_agent_for_action(user_content) else _build_action_proposal(user_content)
    if proposal is not None:
        response_text = proposal["preview"]
        create_message(
            user_id=user_id,
            role="assistant",
            content=response_text,
            tool_calls=proposal,
        )
        return BotTurnResult(response=response_text)

    history = _messages_for_claude(list_messages(user_id, limit=MAX_HISTORY_MESSAGES))
    draft_kind = _draft_kind(user_content)
    system_prompt = prompts.babyg_system_prompt(
        draft_kind=draft_kind,
        task_kind=_task_kind(user_content, draft_kind=draft_kind),
    )

    tool_calls: list[dict[str, Any]] = []
    pending_action: dict[str, Any] | None = None
    try:
        claude_response, tool_calls, pending_action = _run_agent_loop(
            user_id=user_id,
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
    except Exception:
        # Belt for the agent loop. ClaudeNotConfigured / ClaudeCallError
        # above cover the known upstream failures; this catches anything
        # else (tool-result serialization edge cases, unexpected response
        # shapes, etc.) so a single bad turn never returns a 500 to the
        # creator. The traceback is logged server-side via .exception().
        # The creator message is still persisted (above), and the
        # assistant message we write below explains the failure without
        # inventing stats or other context.
        logger.exception("bot turn failed unexpectedly")
        response_text = (
            "i hit a snag pulling that. your message is saved — "
            "try a slightly narrower ask?"
        )
        input_tokens = 0
        output_tokens = 0

    create_message(
        user_id=user_id,
        role="assistant",
        content=response_text,
        tool_calls=pending_action or tool_calls or None,
    )
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


def _run_agent_loop(
    *,
    user_id: str,
    system_prompt: str,
    messages: list[dict[str, Any]],
) -> tuple[
    anthropic_client.ClaudeResponse,
    list[dict[str, Any]],
    dict[str, Any] | None,
]:
    tool_calls: list[dict[str, Any]] = []
    pending_action: dict[str, Any] | None = None
    working_messages = list(messages)
    input_tokens = 0
    output_tokens = 0

    for _iteration in range(MAX_TOOL_ITERATIONS):
        response = anthropic_client.complete_chat(
            system_prompt=system_prompt,
            messages=working_messages,
            tools=prompts.BOT_TOOL_DEFINITIONS,
        )
        input_tokens += response.input_tokens
        output_tokens += response.output_tokens
        content = response.content or []
        tool_uses = [block for block in content if block.get("type") == "tool_use"]
        if response.stop_reason != "tool_use" or not tool_uses:
            return (
                anthropic_client.ClaudeResponse(
                    text=response.text,
                    content=response.content,
                    stop_reason=response.stop_reason,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                ),
                tool_calls,
                pending_action,
            )

        working_messages.append({"role": "assistant", "content": content})
        results: list[dict[str, Any]] = []
        for tool_use in tool_uses:
            tool_name = str(tool_use.get("name") or "")
            tool_input = tool_use.get("input")
            if not isinstance(tool_input, dict):
                tool_input = {}
            result = _execute_agent_tool(
                user_id=user_id,
                name=tool_name,
                tool_input=tool_input,
                pending_action=pending_action,
            )
            if result.get("pending_action") and pending_action is None:
                candidate = result["pending_action"]
                if isinstance(candidate, dict):
                    pending_action = candidate
            tool_calls.append(
                {
                    "kind": result["kind"],
                    "name": tool_name,
                    "input": tool_input,
                    "ok": result["ok"],
                }
            )
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use.get("id"),
                    "content": json.dumps(result["content"], default=str),
                    "is_error": not result["ok"],
                }
            )
        working_messages.append({"role": "user", "content": results})

    fallback = anthropic_client.ClaudeResponse(
        text=(
            "I pulled a few babyg records, but I need a cleaner pass to answer. "
            "Try narrowing the ask to one creator task."
        ),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )
    return fallback, tool_calls, pending_action


def _execute_agent_tool(
    *,
    user_id: str,
    name: str,
    tool_input: dict[str, Any],
    pending_action: dict[str, Any] | None,
) -> dict[str, Any]:
    if name == "create_booking":
        return _stage_create_booking_tool(tool_input, pending_action=pending_action)
    if name == "create_gmail_draft":
        return _stage_create_gmail_draft_tool(
            tool_input, pending_action=pending_action, user_id=user_id
        )
    return _execute_read_tool(user_id=user_id, name=name, tool_input=tool_input)


def _execute_read_tool(
    *, user_id: str, name: str, tool_input: dict[str, Any]
) -> dict[str, Any]:
    content: Any
    try:
        if name == "read_my_profile":
            content = read_only.read_my_profile(user_id)
        elif name == "read_intel_feed":
            profile = read_only.read_my_profile(user_id)
            content = read_only.read_intel_feed(
                niches=_as_tool_list(profile.get("niches")),
                tier=str(profile.get("tier") or "basic"),
                limit=tool_input.get("limit", 5),
            )
        elif name == "read_my_calendar":
            content = read_only.read_my_calendar(
                user_id, limit=tool_input.get("limit", 5)
            )
        elif name == "read_my_dms":
            content = read_only.read_my_dms(user_id, limit=tool_input.get("limit", 5))
        elif name == "read_my_receipts":
            content = read_only.read_my_receipts(
                user_id, limit=tool_input.get("limit", 5)
            )
        elif name == "read_my_performance":
            content = read_only.read_my_performance(
                user_id, limit=tool_input.get("limit", 3)
            )
        elif name == "web_search":
            content = _run_web_search(user_id=user_id, tool_input=tool_input)
        elif name == "read_my_instagram_stats":
            content = _run_instagram_stats(user_id=user_id, tool_input=tool_input)
        elif name == "read_my_gmail":
            content = _run_gmail_inbox(user_id=user_id, tool_input=tool_input)
        elif name == "read_creator_directory":
            content = read_only.read_creator_directory(
                user_id, limit=tool_input.get("limit", 6)
            )
        else:
            return {
                "kind": "read_tool",
                "ok": False,
                "content": f"Unknown read-only tool: {name}",
            }
    except Exception:
        logger.exception("read-only bot tool failed: %s", name)
        return {"kind": "read_tool", "ok": False, "content": f"{name} failed."}
    return {"kind": "read_tool", "ok": True, "content": content}


def _stage_create_booking_tool(
    tool_input: dict[str, Any], *, pending_action: dict[str, Any] | None
) -> dict[str, Any]:
    if pending_action is not None:
        return {
            "kind": "write_tool",
            "ok": False,
            "content": "A local action is already pending for this turn.",
        }
    payload = _booking_payload_from_tool(tool_input)
    missing = [field for field in ("title", "starts_at") if not payload.get(field)]
    if missing:
        return {
            "kind": "write_tool",
            "ok": False,
            "content": f"Missing required booking fields: {', '.join(missing)}.",
        }
    proposal = _proposal_for_action(action_type="create_booking", payload=payload)
    return {
        "kind": "write_tool",
        "ok": True,
        "content": {
            "status": "pending_confirmation",
            "action_type": "create_booking",
            "preview": proposal["preview"],
            "message": "No booking was saved. The creator must confirm the action card.",
        },
        "pending_action": proposal,
    }


def _stage_create_gmail_draft_tool(
    tool_input: dict[str, Any],
    *,
    pending_action: dict[str, Any] | None,
    user_id: str,
) -> dict[str, Any]:
    """Stage a Gmail draft proposal. NEVER calls Gmail itself — that
    only happens after the creator confirms via the action card."""
    if pending_action is not None:
        return {
            "kind": "write_tool",
            "ok": False,
            "content": "A local action is already pending for this turn.",
        }
    if not google_calendar.is_configured():
        return {
            "kind": "write_tool",
            "ok": False,
            "content": "Gmail integration is not configured on this server yet.",
        }
    try:
        connection = oauth_connections.get_google_connection(user_id)
    except Exception:
        logger.exception("gmail connection lookup failed (draft stage)")
        connection = None
    if not connection or not oauth_connections.google_gmail_compose_connected(
        connection
    ):
        return {
            "kind": "write_tool",
            "ok": False,
            "content": (
                "Gmail draft access requires the creator to reconnect Gmail "
                "with the compose scope from /creator/profile/settings."
            ),
        }
    payload = _gmail_draft_payload_from_tool(tool_input)
    missing = [k for k in ("to", "subject", "body") if not payload.get(k)]
    if missing:
        return {
            "kind": "write_tool",
            "ok": False,
            "content": f"Missing required Gmail draft fields: {', '.join(missing)}.",
        }
    proposal = _proposal_for_action(action_type="create_gmail_draft", payload=payload)
    return {
        "kind": "write_tool",
        "ok": True,
        "content": {
            "status": "pending_confirmation",
            "action_type": "create_gmail_draft",
            "preview": proposal["preview"],
            "message": (
                "No Gmail draft was created. The creator must confirm "
                "the action card. babyg never sends."
            ),
        },
        "pending_action": proposal,
    }


def _gmail_draft_payload_from_tool(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Project the bot's tool_input into the draft payload shape.

    Truncation here mirrors the hard caps in google_gmail so the
    preview shown to the creator matches what would actually land
    in Gmail on confirm."""
    to = str(tool_input.get("to") or "").strip()[: google_gmail.DRAFT_TO_MAX_CHARS]
    subject = (
        str(tool_input.get("subject") or "").strip()[
            : google_gmail.DRAFT_SUBJECT_MAX_CHARS
        ]
    )
    body = str(tool_input.get("body") or "")[: google_gmail.DRAFT_BODY_MAX_CHARS]
    thread_id = str(tool_input.get("thread_id") or "").strip() or None
    payload: dict[str, Any] = {"to": to, "subject": subject, "body": body}
    if thread_id:
        payload["thread_id"] = thread_id
    return payload


def _run_web_search(*, user_id: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute one Tavily search inside the agent loop.

    Returns a structured payload Claude can read. Three failure modes
    map to `available=False` with a reason — never a thrown exception —
    so the agent loop keeps the turn alive and can answer from local
    context. Never leaks the API key or query bodies into logs.
    """
    query = str(tool_input.get("query") or "").strip()
    if not query:
        return {
            "available": False,
            "reason": "empty query",
        }
    if not tavily.is_configured():
        return {
            "available": False,
            "reason": "web_search is not configured on this server yet",
        }
    used_today = _bump_web_search_counter(user_id)
    if used_today > WEB_SEARCH_DAILY_CAP:
        return {
            "available": False,
            "reason": (
                f"daily web_search cap reached ({WEB_SEARCH_DAILY_CAP}/day). "
                "answer from local context for the rest of today."
            ),
        }
    try:
        hits = tavily.search(query, max_results=tool_input.get("max_results", 5))
    except tavily.TavilyNotConfiguredError:
        return {
            "available": False,
            "reason": "web_search is not configured on this server yet",
        }
    except tavily.TavilyCallError:
        return {
            "available": False,
            "reason": "web_search upstream failed — try again or answer from local context",
        }
    return {
        "available": True,
        "query": query,
        "results": [
            {
                "title": hit.title,
                "url": hit.url,
                "snippet": hit.snippet,
                "published": hit.published,
            }
            for hit in hits
        ],
    }


def _run_instagram_stats(*, user_id: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute one read_my_instagram_stats turn inside the agent loop.

    Mirrors `_run_web_search`: never raises, never crashes the bot
    turn. Four failure modes map to `available=False` with a reason
    so Claude can route around them per the stats-reality-check
    section of the system prompt — and so we never claim live IG
    data exists when it doesn't.

    Never logs tokens — token is opaque to this function (only
    `oauth_connections.access_token_for_instagram` reads it from the
    DB and hands it straight to the Graph helpers).
    """
    if not instagram_meta.is_configured():
        return {
            "available": False,
            "reason": "instagram integration is not configured on this server yet",
        }
    used_today = _bump_instagram_stats_counter(user_id)
    if used_today > INSTAGRAM_STATS_DAILY_CAP:
        return {
            "available": False,
            "reason": (
                f"daily instagram stats cap reached "
                f"({INSTAGRAM_STATS_DAILY_CAP}/day). fall back to "
                "saved performance data for the rest of today."
            ),
        }
    try:
        token = oauth_connections.access_token_for_instagram(user_id)
    except Exception:
        logger.exception("instagram access token lookup failed")
        token = None
    if not token:
        return {
            "available": False,
            "reason": (
                "instagram isn't connected for this creator. they can "
                "connect it from /creator/profile/settings if they have "
                "an Instagram Business or Creator account linked to a "
                "Facebook Page."
            ),
        }
    connection = oauth_connections.get_instagram_connection(user_id)
    ig_user_id = oauth_connections.instagram_account_id(connection)
    if not ig_user_id:
        return {
            "available": False,
            "reason": "instagram connection is missing the business account id",
        }
    try:
        media = instagram_meta.get_user_media(
            token, ig_user_id=ig_user_id, limit=tool_input.get("limit", 5)
        )
    except instagram_meta.InstagramNotConfiguredError:
        return {
            "available": False,
            "reason": "instagram integration is not configured on this server yet",
        }
    except instagram_meta.InstagramError:
        return {
            "available": False,
            "reason": "instagram graph api failed — try again later",
        }
    results: list[dict[str, Any]] = []
    for item in media:
        # Per-post insights are best-effort: stories vs feed vs reels
        # support different metric sets. Missing metrics come back as
        # None so the model renders only what's real.
        insights: dict[str, int | None] = {}
        try:
            insights = instagram_meta.get_media_insights(
                token, media_id=item.media_id
            )
        except instagram_meta.InstagramError:
            # Skip insights for this post but keep the metadata —
            # like_count + comments_count are still in the media row.
            insights = {}
        results.append(
            {
                "media_id": item.media_id,
                "caption": item.caption,
                "media_type": item.media_type,
                "permalink": item.permalink,
                "timestamp": item.timestamp,
                "like_count": item.like_count,
                "comments_count": item.comments_count,
                "insights": insights,
            }
        )
    return {"available": True, "results": results}


def _run_gmail_inbox(*, user_id: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute one read_my_gmail turn inside the agent loop.

    Same shape as _run_instagram_stats and _run_web_search: every
    failure mode maps to `available=False` with a `reason`, so the
    bot loop survives a Gmail outage and can still answer from local
    context. Never raises. Never logs tokens, subjects, or bodies.
    """
    if not google_calendar.is_configured():
        return {
            "available": False,
            "reason": "gmail integration is not configured on this server yet",
        }
    used_today = _bump_gmail_inbox_counter(user_id)
    if used_today > GMAIL_INBOX_DAILY_CAP:
        return {
            "available": False,
            "reason": (
                f"daily gmail inbox cap reached "
                f"({GMAIL_INBOX_DAILY_CAP}/day). fall back to local "
                "context for the rest of today."
            ),
        }
    try:
        connection = oauth_connections.get_google_connection(user_id)
    except Exception:
        logger.exception("gmail connection lookup failed")
        connection = None
    if not connection or not oauth_connections.google_gmail_connected(connection):
        return {
            "available": False,
            "reason": (
                "gmail isn't connected for this creator. they can "
                "connect it from /creator/profile/settings under the "
                "Google integration card."
            ),
        }
    try:
        token = oauth_connections.access_token_for_google(user_id)
    except Exception:
        logger.exception("gmail access token lookup failed")
        token = None
    if not token:
        return {
            "available": False,
            "reason": "gmail token unavailable — the creator may need to reconnect",
        }
    try:
        threads = google_gmail.list_recent_threads(
            token, limit=tool_input.get("limit", 5)
        )
    except google_gmail.GmailUnauthorizedError:
        return {
            "available": False,
            "reason": (
                "gmail token rejected — the creator may need to "
                "reconnect Gmail from /creator/profile/settings"
            ),
        }
    except google_gmail.GmailNotConnectedError:
        return {
            "available": False,
            "reason": (
                "gmail scope is missing — the creator should reconnect "
                "Gmail and tick the Gmail box on the picker"
            ),
        }
    except google_gmail.GmailError:
        return {
            "available": False,
            "reason": "gmail api failed — try again later",
        }
    results = [
        {
            "thread_id": t.thread_id,
            "snippet": t.snippet,
            "is_unread": t.is_unread,
            "messages": [
                {
                    "message_id": m.message_id,
                    "from": m.from_,
                    "to": m.to,
                    "subject": m.subject,
                    "snippet": m.snippet,
                    "body_text": m.body_text,
                    "internal_date": m.internal_date,
                    "is_unread": m.is_unread,
                }
                for m in t.messages
            ],
        }
        for t in threads
    ]
    return {"available": True, "results": results}


def _as_tool_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


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


def _messages_for_claude(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    for row in rows:
        role = row.get("role")
        if role not in ("user", "assistant"):
            continue
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        messages.append({"role": role, "content": content})
    return messages


def _should_use_agent_for_action(content: str) -> bool:
    return _action_type(content) == "create_booking"


def _build_action_proposal(content: str) -> dict[str, Any] | None:
    action_type = _action_type(content)
    if action_type is None:
        return None
    payload = _action_payload(action_type=action_type, content=content)
    return _proposal_for_action(action_type=action_type, payload=payload)


def _proposal_for_action(*, action_type: str, payload: dict[str, Any]) -> dict[str, Any]:
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


def _booking_payload_from_tool(tool_input: dict[str, Any]) -> dict[str, Any]:
    booking_type = str(tool_input.get("type") or "event").strip().lower()
    if booking_type not in ("event", "collab", "brand", "reminder"):
        booking_type = "event"
    title = str(tool_input.get("title") or "").strip()[:140]
    starts_at = str(tool_input.get("starts_at") or "").strip()
    ends_at = tool_input.get("ends_at")
    notes = tool_input.get("notes")
    venue_name = tool_input.get("venue_name")
    return {
        "title": title,
        "type": booking_type,
        "starts_at": starts_at,
        "ends_at": str(ends_at).strip() if ends_at else None,
        "notes": str(notes).strip()[:2000] if notes else None,
        "venue_name": str(venue_name).strip()[:200] if venue_name else None,
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
    if action_type == "create_gmail_draft":
        body_preview = (payload.get("body") or "")[:200]
        if len(payload.get("body") or "") > 200:
            body_preview = body_preview.rstrip() + "…"
        return (
            "I can save this Gmail draft after you confirm:\n\n"
            f"To: {payload['to']}\n"
            f"Subject: {payload['subject']}\n\n"
            f"{body_preview}\n\n"
            "Nothing has been saved yet. babyg never sends — you "
            "review and send from Gmail yourself."
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
    if action_type == "create_gmail_draft":
        return _execute_gmail_draft(user_id=user_id, payload=payload)
    return None


def _execute_gmail_draft(
    *, user_id: str, payload: dict[str, Any]
) -> str | None:
    """Call Gmail drafts.create with the confirmed payload. Returns
    the draft id, or None on any failure — caller surfaces the
    'couldn't save' message in that case."""
    try:
        connection = oauth_connections.get_google_connection(user_id)
    except Exception:
        logger.exception("gmail draft confirm: connection lookup failed")
        return None
    if not connection or not oauth_connections.google_gmail_compose_connected(
        connection
    ):
        return None
    try:
        token = oauth_connections.access_token_for_google(user_id)
    except Exception:
        logger.exception("gmail draft confirm: token lookup failed")
        return None
    if not token:
        return None
    try:
        return google_gmail.create_draft(
            token,
            to=str(payload.get("to") or ""),
            subject=str(payload.get("subject") or ""),
            body=str(payload.get("body") or ""),
            thread_id=(payload.get("thread_id") or None),
        )
    except google_gmail.GmailError:
        logger.exception("gmail drafts.create failed")
        return None


def _success_message(action_type: str, record_id: str) -> str:
    if action_type == "create_booking":
        return f"Done. I created that local calendar item in babyg. Record: {record_id}"
    if action_type == "create_content_reminder":
        return f"Done. I created that local content reminder in babyg. Record: {record_id}"
    if action_type == "submit_creator_listing":
        return f"Done. I submitted that creator listing in babyg. Record: {record_id}"
    if action_type == "create_gmail_draft":
        return (
            f"Done. Gmail draft saved (id {record_id}). Open Gmail to review "
            "and send — babyg does not send."
        )
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


def _task_kind(content: str, *, draft_kind: DraftKind | None = None) -> TaskKind | None:
    lowered = content.lower()
    if any(
        marker in lowered
        for marker in (
            "hot drop",
            "hot drops",
            "intel",
            "trend",
            "venue",
            "what's hot",
            "what is hot",
            "act on",
        )
    ):
        return "hot_drops"
    if any(
        marker in lowered
        for marker in (
            "offer",
            "rate",
            "usage",
            "whitelisting",
            "exclusivity",
            "deliverables",
            "brand email",
            "brand message",
        )
    ):
        return "offer_review"
    if any(
        marker in lowered
        for marker in (
            "weekly plan",
            "content plan",
            "what should i post",
            "post today",
            "plan my week",
            "content calendar",
        )
    ):
        return "planning"
    if any(
        marker in lowered
        for marker in (
            "calendar",
            "schedule",
            "booking",
            "deadline",
            "reminder",
            "remind me",
        )
    ):
        return "calendar"
    if any(
        marker in lowered
        for marker in (
            "collab",
            "creator dm",
            "creator message",
            "network",
            "directory",
            "connect",
        )
    ):
        return "networking"
    if any(
        marker in lowered
        for marker in (
            "stats",
            "performance",
            "analytics",
            "growth",
            "engagement",
            "followers",
            "recap",
        )
    ):
        return "stats"
    if draft_kind == "content_plan":
        return "planning"
    if draft_kind in ("brand_reply", "negotiation"):
        return "offer_review"
    if draft_kind == "creator_dm":
        return "networking"
    return None
