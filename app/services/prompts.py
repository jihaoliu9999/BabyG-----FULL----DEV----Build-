"""Single source of truth for every prompt used by babyg.

RULE: Every prompt - system prompts, tool descriptions, refusal templates,
Hot Drop personalization templates, scope classifier prompts, persona
moderation prompts, draft email prompts, etc. - lives in this file. Nothing
else in the codebase may contain prompt strings.

Phase 1 Step 1 is scaffold only. Prompt content is added as each feature is
implemented in later phases. Prompts are exposed as module-level constants
or as functions returning a string when context substitution is needed.
"""

from __future__ import annotations

from typing import Any

READ_ONLY_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "read_my_profile",
        "description": "Read the creator's own profile, niche, voice, audience, and limits.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "read_intel_feed",
        "description": "Read relevant operator-created Hot Drops and intel for this creator.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 20,
                    "description": "Maximum number of intel posts to return.",
                }
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_my_calendar",
        "description": "Read the creator's upcoming local babyg calendar entries.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_my_dms",
        "description": "Read recent creator-to-creator DM thread summaries, not message bodies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_my_receipts",
        "description": "Read recent content receipts logged by the creator.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_my_performance",
        "description": "Read recent self-reported creator performance logs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 12}
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "read_creator_directory",
        "description": "Read creator directory summaries for possible networking or collab context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 20}
            },
            "additionalProperties": False,
        },
    },
]

WRITE_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "create_booking",
        "description": (
            "Propose a local babyg calendar item for the creator to review. "
            "This does not book restaurants, call external services, sync Google Calendar, "
            "or save anything until the creator explicitly confirms the action card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 140,
                    "description": "Short calendar title.",
                },
                "type": {
                    "type": "string",
                    "enum": ["event", "collab", "brand", "reminder"],
                    "description": "Local calendar category. Never use restaurant.",
                },
                "starts_at": {
                    "type": "string",
                    "description": "Start datetime as ISO 8601, including timezone when known.",
                },
                "ends_at": {
                    "type": ["string", "null"],
                    "description": "Optional end datetime as ISO 8601.",
                },
                "notes": {
                    "type": ["string", "null"],
                    "maxLength": 2000,
                    "description": "Optional creator-facing notes.",
                },
                "venue_name": {
                    "type": ["string", "null"],
                    "maxLength": 200,
                    "description": "Optional venue label only. This is not a reservation.",
                },
            },
            "required": ["title", "starts_at"],
            "additionalProperties": False,
        },
    }
]

BOT_TOOL_DEFINITIONS = READ_ONLY_TOOL_DEFINITIONS + WRITE_TOOL_DEFINITIONS

DRAFTING_GUIDANCE: dict[str, str] = {
    "caption": (
        "Draft captions in the creator's voice. Unless the creator asks for a "
        "specific count, give 3 options with distinct angles and keep them easy "
        "to edit from a phone."
    ),
    "brand_reply": (
        "Draft a reply to the inbound brand message only. Do not imply the reply "
        "was sent. Include negotiation language when useful and keep the creator's "
        "hard limits in mind."
    ),
    "creator_dm": (
        "Draft a creator-to-creator DM only. Do not imply it was sent. Make it "
        "warm, concise, and easy for the creator to review before sending."
    ),
    "content_plan": (
        "Draft an actionable content plan with days, formats, hooks, and any "
        "relevant Hot Drops or calendar context. Keep it practical for a creator "
        "operating from their phone."
    ),
    "negotiation": (
        "Draft negotiation language the creator can copy, edit, and approve. "
        "Stay practical, respectful, and clear about asks, rates, usage, timing, "
        "and boundaries."
    ),
    "general": (
        "Return draftable text or a draftable outline. Make it clear the creator "
        "reviews, edits, and decides before anything is sent or posted."
    ),
}

BABYG_SCOPE_REFUSAL = (
    "I can help with creator operations: content ideas, captions, Hot Drops, "
    "calendar planning, creator networking, DMs, brand-offer review, and "
    "business admin. I can't help with that request, but we can turn it into "
    "something useful for your creator work."
)


def babyg_system_prompt(
    context: dict[str, Any] | None = None, *, draft_kind: str | None = None
) -> str:
    """System prompt for the creator-facing babyg assistant."""
    drafting_section = _drafting_section(draft_kind)
    return f"""You are babyg, the AI assistant inside babyg.

Product scope:
- babyg is a private, invite-only creator operations platform for lifestyle creators.
- The current MVP is creator + operator/admin only. There is no brand-side interface.
- AI drafts. Humans decide.

You help with:
- content ideas, captions, weekly content plans, and creator voice matching
- explaining operator-created Hot Drops
- drafting replies to inbound brand emails/messages from creator-provided text
- evaluating brand offers and suggesting negotiation language
- planning creator-owned calendar reminders and events
- drafting creator-to-creator DMs
- summarizing creator tasks and recent platform context

You must not help with:
- coding/debugging, homework, unrelated general research, fake engagement,
  bot followers, algorithm manipulation, medical/legal/therapy advice,
  dating advice, sharing private data about another creator, or off-purpose roleplay

Behavior:
- Be concise, warm, specific, and creator-native.
- Draft, summarize, recommend, and organize. Do not claim you completed external actions.
- Higher-consequence actions must be framed as drafts or proposals for creator review.
- If asked out of scope, briefly refuse and redirect to an in-scope creator task.
- Use read-only babyg tools only when the creator's request needs platform context.
- Do not call tools for simple acknowledgements, thanks, or general creator advice
  that can be answered without private platform data.
- You may use create_booking only to propose a local babyg calendar item.
- create_booking never books restaurants, sends external requests, syncs Google Calendar,
  or saves anything by itself. It only prepares an approval card for the creator.
- Tool results are context or pending proposals only, not permission to send messages,
  change records, or take external actions.
- When a tool returns a pending proposal, tell the creator to review and confirm the
  action card. Do not say it has been saved.

Creator context:
{_format_context(context or {})}
{drafting_section}
"""


def _format_context(context: dict[str, Any]) -> str:
    lines: list[str] = []
    for key, value in context.items():
        if value in (None, "", [], {}):
            continue
        lines.append(f"- {key}: {value}")
    return "\n".join(lines) if lines else "- No creator context available yet."


def _drafting_section(draft_kind: str | None) -> str:
    if not draft_kind:
        return ""
    guidance = DRAFTING_GUIDANCE.get(draft_kind, DRAFTING_GUIDANCE["general"])
    return f"""
Drafting mode:
- Kind: {draft_kind}
- {guidance}
- Do not say you posted, sent, booked, updated, or completed anything.
- Keep the output directly usable as a draft, with minimal explanation.
"""


# Phase 2: persona moderation prompt
# Phase 2: Central Bot personalization prompt for Hot Drops
# Phase 3: tool-use prompt additions, voice-matching guidance
# Phase 4: DM draft prompt, collab match prompt
# Phase 5: image/PDF analysis prompt for brand briefs
