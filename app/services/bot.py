"""babyg assistant orchestration and message persistence."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Literal

from postgrest.exceptions import APIError as PostgrestAPIError

from app.core import supabase_client
from app.integrations import anthropic_client
from app.services import bookings, intel, performance, profiles, prompts, receipts

logger = logging.getLogger(__name__)

MessageRole = Literal["user", "assistant", "system"]

MAX_USER_MESSAGE_CHARS = 4000
MAX_HISTORY_MESSAGES = 20

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

    context = build_context(user_id)
    history = _messages_for_claude(list_messages(user_id, limit=MAX_HISTORY_MESSAGES))
    system_prompt = prompts.babyg_system_prompt(context)

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


def build_context(user_id: str) -> dict[str, Any]:
    profile = profiles.get_creator_profile(user_id) or {}
    profile_niches = profile.get("niches") or []
    tier = profile.get("tier") or "basic"
    return {
        "name": profile.get("full_name"),
        "instagram": profile.get("instagram_handle"),
        "city": profile.get("neighborhood") or "Miami",
        "niches": profile_niches,
        "formats": profile.get("content_formats"),
        "topics": profile.get("topics"),
        "follower_range": profile.get("follower_range"),
        "engagement_range": profile.get("engagement_range"),
        "years_creating": profile.get("creator_tenure"),
        "hard_limits": profile.get("hard_limits"),
        "writing_samples": _summarize_list(profile.get("writing_samples"), limit=3),
        "active_hot_drops": _summarize_intel(
            intel.feed_for_creator(niches=profile_niches, tier=tier)[:5]
        ),
        "upcoming_calendar": _summarize_bookings(
            bookings.list_for_user(user_id, horizon="upcoming", limit=5)
        ),
        "recent_receipts": _summarize_receipts(receipts.list_for_user(user_id, limit=5)),
        "recent_performance": _summarize_performance(
            performance.list_for_user(user_id, limit=3)
        ),
    }


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


def _scope_flag(content: str) -> str | None:
    lowered = content.lower()
    if any(keyword in lowered for keyword in OUT_OF_SCOPE_KEYWORDS):
        return "scope"
    return None


def _summarize_list(value: Any, *, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:500] for item in value[:limit] if str(item).strip()]


def _summarize_intel(rows: list[dict[str, Any]]) -> list[str]:
    return [
        f"{row.get('category')}: {row.get('title')} - {str(row.get('body') or '')[:240]}"
        for row in rows
    ]


def _summarize_bookings(rows: list[dict[str, Any]]) -> list[str]:
    return [
        f"{row.get('starts_at')}: {row.get('title')} ({row.get('type')})"
        for row in rows
    ]


def _summarize_receipts(rows: list[dict[str, Any]]) -> list[str]:
    return [
        f"{row.get('post_type')}: {row.get('caption_excerpt') or row.get('post_url')}"
        for row in rows
    ]


def _summarize_performance(rows: list[dict[str, Any]]) -> list[str]:
    return [
        (
            f"{row.get('week_start_date')}: engagement={row.get('engagement_rate')}, "
            f"followers={row.get('follower_delta')}, "
            f"deal_value={row.get('active_brand_deals_value')}"
        )
        for row in rows
    ]
