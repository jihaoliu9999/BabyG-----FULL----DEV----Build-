"""Narrow Anthropic Claude client for the babyg assistant.

The rest of the app talks to this module instead of importing Anthropic
directly. That keeps API-key handling server-side and gives tests a small,
mockable surface.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, cast

from app.config import get_settings
from app.core.external_timing import time_external

logger = logging.getLogger(__name__)


class ClaudeNotConfiguredError(RuntimeError):
    """Raised when the app asks Claude to run without an API key."""


class ClaudeCallError(RuntimeError):
    """Raised when Claude is configured but the upstream call fails."""


@dataclass(frozen=True)
class ClaudeResponse:
    text: str
    content: list[dict[str, Any]] | None = None
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def complete_chat(
    *,
    system_prompt: str,
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]] | None = None,
    max_tokens: int = 900,
) -> ClaudeResponse:
    """Return one blocking Claude response.

    Phase 2 starts with a blocking turn; SSE streaming lands after the base
    conversation loop is stable.
    """
    settings = get_settings()
    if not settings.anthropic_api_key:
        raise ClaudeNotConfiguredError("ANTHROPIC_API_KEY is not set")

    try:
        from anthropic import Anthropic  # type: ignore[import-not-found]

        client = Anthropic(api_key=settings.anthropic_api_key)
        create_kwargs: dict[str, Any] = {
            "model": settings.anthropic_model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": cast(Any, messages),
        }
        if tools:
            create_kwargs["tools"] = cast(Any, tools)
        with time_external("anthropic"):
            response = client.messages.create(**create_kwargs)
    except ClaudeNotConfiguredError:
        raise
    except Exception as exc:  # pragma: no cover - exercised via service tests
        logger.exception("anthropic messages.create failed")
        raise ClaudeCallError("Claude request failed") from exc

    return ClaudeResponse(
        text=_extract_text(response),
        content=_extract_content_blocks(response),
        stop_reason=getattr(response, "stop_reason", None),
        input_tokens=int(getattr(getattr(response, "usage", None), "input_tokens", 0) or 0),
        output_tokens=int(
            getattr(getattr(response, "usage", None), "output_tokens", 0) or 0
        ),
    )


def _extract_text(response: Any) -> str:
    parts: list[str] = []
    for block in getattr(response, "content", []) or []:
        text = getattr(block, "text", None)
        if isinstance(text, str):
            parts.append(text)
    # Separate distinct text blocks with a blank line so paragraph
    # breaks survive when Claude returns multiple text blocks
    # (e.g. interleaved with tool use).
    return "\n\n".join(parts).strip()


def _extract_content_blocks(response: Any) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for block in getattr(response, "content", []) or []:
        block_type = getattr(block, "type", None)
        if block_type == "text":
            blocks.append({"type": "text", "text": getattr(block, "text", "")})
        elif block_type == "tool_use":
            blocks.append(
                {
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": getattr(block, "input", {}) or {},
                }
            )
    return blocks
