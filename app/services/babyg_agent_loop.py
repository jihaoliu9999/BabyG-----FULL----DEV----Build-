"""The babyg background agent loop.

One `run_cycle(user_id)` fires once per creator per 5-min cron slot.
Per creator it:

  1. Loads the creator profile (once, hands to every autonomy check).
  2. Short-circuits with 'skipped_over_cap' if today's spend >= cap.
  3. Reads the world (agent_tools.observe) and computes a delta.
     If nothing new -> 'skipped_no_delta', no LLM call, no tokens.
  4. Loads the durable memory summary.
  5. Builds a system prompt from memory + observation + tool defs.
  6. Calls claude with the write tools as tool definitions.
  7. Executes each tool_use block via agent_writes; failures land
     as {ok: False, reason: '...'} entries in tools_called, not
     exceptions.
  8. Records the cycle in agent_cycles (always) and increments
     agent_daily_spend by the cycle's real token usage.

MVP scope:
- **Single-turn.** claude gets the full observation up-front, decides
  writes, we execute. we do NOT hand tool_result back for a second
  turn yet — that lands in v2 when we add read tools claude can
  invoke to dig deeper on demand.
- **Write tools only** are surfaced to claude:
  drop_nudge, rewrite_memory, update_deal_stage, mark_draft_stale.
- **Model default** is haiku (cheap, fast). override via
  BABYG_AGENT_MODEL env var.

Not in scope this commit:
- Gmail auto-send tool (needs the "obviously safe" pattern
  classifier from #6's design notes).
- Calendar hold tool.
- Multi-turn tool-result iteration.
- Web search / external lookup tools.

Failure semantics:
- claude call raises      -> record cycle status='failed' with
                             error_class + message; never propagate.
- tool call raises         -> that tool's result is {ok: False,
                             reason: 'exception', class: <name>};
                             cycle continues with the next tool.
- cost record write fails  -> logged but doesn't fail the cycle.
- agent_cycles write fails -> logged; cycle work already ran.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

from app.core import supabase_client
from app.integrations.anthropic_client import (
    ClaudeCallError,
    ClaudeNotConfiguredError,
    complete_chat,
)
from app.services import (
    agent_autonomy,
    agent_cost,
    agent_cycles,
    agent_memory,
    agent_tools,
    agent_writes,
    profiles,
)

logger = logging.getLogger(__name__)

# Max creators handled per top-level run_for_all_creators() invocation.
# Railway cron fires every 5 min; with ~300 active creators and one
# LLM call each, this stays inside a single 5-min slot with headroom.
MAX_CREATORS_PER_LOOP = 300

# Default model. Haiku is fast + cheap; the pre-filter keeps the
# per-cycle claude call rare.
DEFAULT_AGENT_MODEL = "claude-haiku-4-5-20251001"

# Response cap. babyg is not verbose here — one paragraph of
# reasoning + tool calls, at most.
AGENT_MAX_TOKENS = 700


def agent_model() -> str:
    return (os.environ.get("BABYG_AGENT_MODEL") or DEFAULT_AGENT_MODEL).strip()


# --- Prompt shaping --------------------------------------------------

_SYSTEM_PROMPT_HEADER = """you are babyg, an AI manager for a solo creator.

you run in the background every few minutes. your job is to watch what changed since you last thought, and either:

- take a bounded action on the creator's behalf (drop a nudge, update memory, flip a deal stage, mark a draft stale), or
- stay silent this cycle.

speak in lowercase, no em dashes, no exclamations, no emojis. talk like a competent chief of staff who happens to text.

house rules:
- do not fabricate people, brands, DMs, emails, or deals. only reason about what's in the observation below.
- if the observation is empty or already handled, do NOT drop a nudge — silence is the correct move.
- when you rewrite memory, add ONE line of change_reason that names the concrete evidence you saw.
- tools are your only way to affect anything. free-text output is context for the trace only.
"""


def _build_prompt(
    *,
    memory: dict[str, Any] | None,
    observation: dict[str, Any],
    autonomy_settings: dict[str, bool],
) -> tuple[str, str]:
    """Return (system_prompt, user_message_content)."""
    memory_line = ""
    if memory and (memory.get("summary") or "").strip():
        memory_line = (
            f"\n\nwhat you already know about this creator (memory v{memory.get('version') or 0}):\n"
            f"{memory['summary']}"
        )
    autonomy_line = (
        "\n\nautonomy settings for this creator:\n"
        f"- can change internal state (deals, memory, drafts): {autonomy_settings['internal_actions']}\n"
        f"- can auto-send gmail replies:                       {autonomy_settings['gmail_auto_send']}\n"
        f"- can create calendar holds:                         {autonomy_settings['calendar_holds']}\n"
        "if a switch is False and you try to call a gated tool, the server refuses. work within what's allowed."
    )
    system_prompt = _SYSTEM_PROMPT_HEADER + memory_line + autonomy_line
    user_content = (
        "here's what has changed / is pending since your last cycle. call the tools you need "
        "and only the tools you need. if nothing warrants an action right now, respond with a "
        "single short text explaining why you're staying silent — do not invent tool calls.\n\n"
        f"observation as of {observation.get('as_of')}:\n"
        f"{_serialize_observation(observation)}"
    )
    return system_prompt, user_content


def _serialize_observation(observation: dict[str, Any]) -> str:
    """Compact human-readable rendering the LLM can parse cheaply.

    Not JSON on purpose: json blows the token count and lists of
    empty arrays are visual noise. Skips empty dimensions entirely.
    """
    lines: list[str] = []
    stale = observation.get("stale_drafts") or []
    if stale:
        lines.append(f"- {len(stale)} draft(s) sitting 14+ days without a save:")
        for row in stale[:10]:
            lines.append(
                f"    * draft {row.get('id')} brand={row.get('brand_name') or 'unknown'} status={row.get('status')}"
            )
    ghosted = observation.get("ghosted_deals") or []
    if ghosted:
        lines.append(f"- {len(ghosted)} deal(s) untouched 14+ days:")
        for row in ghosted[:10]:
            lines.append(
                f"    * deal {row.get('id')} brand={row.get('brand_name') or 'unknown'} stage={row.get('stage')}"
            )
    upcoming = observation.get("upcoming_bookings") or []
    if upcoming:
        lines.append(f"- {len(upcoming)} upcoming booking(s):")
        for row in upcoming[:5]:
            lines.append(
                f"    * '{row.get('title')}' at {row.get('starts_at')}"
                f" venue={row.get('venue_name') or 'tbd'}"
            )
    unread = observation.get("unread_dms") or {}
    if int(unread.get("count") or 0) > 0:
        lines.append(f"- {unread['count']} unread DM(s)")
    pending = observation.get("pending_action_proposals") or {}
    if int(pending.get("count") or 0) > 0:
        buckets = ", ".join(
            f"{n} {kind}" for kind, n in (pending.get("by_action_type") or {}).items()
        )
        lines.append(f"- {pending['count']} pending action proposal(s): {buckets}")
    if not lines:
        return "(no delta this cycle)"
    return "\n".join(lines)


# --- Tool definitions offered to claude ------------------------------

def _tool_definitions() -> list[dict[str, Any]]:
    """The write toolbelt claude sees each cycle. Kept minimal on
    purpose — every added tool multiplies the LLM's option space.
    """
    return [
        {
            "name": "drop_nudge",
            "description": (
                "Send one short message into the creator's babyg thread. "
                "Use only when there's something specific to say. "
                "Do not send greetings or 'checking in' messages."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "body": {"type": "string"},
                    "category": {"type": "string"},
                },
                "required": ["body", "category"],
            },
        },
        {
            "name": "rewrite_memory",
            "description": (
                "Replace the durable creator summary with a new version. "
                "Include a one-line change_reason naming the specific evidence "
                "that justified the rewrite."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "change_reason": {"type": "string"},
                },
                "required": ["summary", "change_reason"],
            },
        },
        {
            "name": "update_deal_stage",
            "description": (
                "Move a deal to a new stage. Use to flip a ghosted deal "
                "to 'stale_or_ghosted' when 14+ days without touch."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "deal_id": {"type": "string"},
                    "to_stage": {"type": "string"},
                },
                "required": ["deal_id", "to_stage"],
            },
        },
        {
            "name": "mark_draft_stale",
            "description": (
                "Flip a draft in 'proposed' or 'edited' status to 'stale'. "
                "Use when the draft has been sitting for 14+ days."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                },
                "required": ["draft_id"],
            },
        },
    ]


_TOOL_DISPATCH = {
    "drop_nudge": lambda user_id, args, profile: agent_writes.drop_nudge(
        user_id,
        body=str(args.get("body") or "").strip(),
        category=str(args.get("category") or "agent"),
        profile=profile,
    ),
    "rewrite_memory": lambda user_id, args, profile: agent_writes.rewrite_memory(
        user_id,
        str(args.get("summary") or "").strip(),
        change_reason=str(args.get("change_reason") or "").strip() or "agent decision",
        profile=profile,
    ),
    "update_deal_stage": lambda user_id, args, profile: agent_writes.update_deal_stage(
        user_id,
        str(args.get("deal_id") or ""),
        str(args.get("to_stage") or ""),
        profile=profile,
    ),
    "mark_draft_stale": lambda user_id, args, profile: agent_writes.mark_draft_stale(
        user_id,
        str(args.get("draft_id") or ""),
        profile=profile,
    ),
}


# --- The loop --------------------------------------------------------

def run_cycle(user_id: str, *, now: datetime | None = None) -> dict[str, Any]:
    """One cycle for one creator. See module docstring."""
    started = now or datetime.now(UTC)
    profile = profiles.get_creator_profile(user_id) or {}

    # 1. Cost cap short-circuit.
    if agent_cost.over_daily_cap(user_id):
        return _record_and_return(
            user_id,
            status="skipped_over_cap",
            started=started,
            skip_reason=f"cap ${agent_cost.daily_cap_usd():.4f}/day met",
        )

    # 2. Observe world state; skip cheaply if nothing new.
    observation = agent_tools.observe(user_id, now=started)
    delta = agent_tools.delta_summary(observation)
    if sum(delta.values()) == 0:
        return _record_and_return(
            user_id,
            status="skipped_no_delta",
            started=started,
            delta=delta,
        )

    autonomy_settings = agent_autonomy.load_settings(user_id, profile=profile)
    memory_row = agent_memory.load(user_id)

    system_prompt, user_content = _build_prompt(
        memory=memory_row,
        observation=observation,
        autonomy_settings=autonomy_settings,
    )
    system_hash = agent_cycles.prompt_hash(system_prompt)

    tools = _tool_definitions()
    model = agent_model()

    try:
        response = complete_chat(
            system_prompt=system_prompt,
            messages=[{"role": "user", "content": user_content}],
            tools=tools,
            max_tokens=AGENT_MAX_TOKENS,
        )
    except ClaudeNotConfiguredError:
        return _record_and_return(
            user_id,
            status="skipped_no_delta",
            started=started,
            skip_reason="anthropic_api_key not configured",
            system_prompt_hash=system_hash,
            delta=delta,
        )
    except ClaudeCallError as exc:
        return _record_and_return(
            user_id,
            status="failed",
            started=started,
            error_class="ClaudeCallError",
            error_message=str(exc),
            system_prompt_hash=system_hash,
            model=model,
            delta=delta,
        )

    tools_called: list[dict[str, Any]] = []
    for block in response.content or []:
        if block.get("type") != "tool_use":
            continue
        name = str(block.get("name") or "")
        args = block.get("input") or {}
        dispatch = _TOOL_DISPATCH.get(name)
        if dispatch is None:
            tools_called.append({"name": name, "outcome": "unknown_tool"})
            continue
        try:
            result = dispatch(user_id, args, profile)
        except Exception as exc:
            logger.exception("babyg_agent_loop.tool_raised name=%s user=%s", name, user_id)
            result = {"ok": False, "reason": "exception", "class": type(exc).__name__}
        tools_called.append({"name": name, "args": args, "outcome": result})

    # Cost accounting: authoritative token counts come from the SDK.
    cost_usd = agent_cost.estimate_cost_usd(
        response.input_tokens, response.output_tokens, model
    )
    agent_cost.record_cycle(
        user_id,
        prompt_tokens=response.input_tokens,
        completion_tokens=response.output_tokens,
        cost_usd=cost_usd,
    )

    return _record_and_return(
        user_id,
        status="ok",
        started=started,
        delta=delta,
        tools_called=tools_called,
        final_response=response.text,
        system_prompt_hash=system_hash,
        model=model,
        prompt_tokens=response.input_tokens,
        completion_tokens=response.output_tokens,
        cost_usd=cost_usd,
    )


def run_for_all_creators(
    *, now: datetime | None = None, limit: int = MAX_CREATORS_PER_LOOP
) -> list[dict[str, Any]]:
    """Iterate active creators and fire run_cycle for each. Called by
    the run_babyg_sweeps.py cron entry point. One list of cycle-record
    results per creator, in call order."""
    ids = _active_creator_ids(limit=limit)
    out: list[dict[str, Any]] = []
    for creator_id in ids:
        try:
            out.append(run_cycle(creator_id, now=now))
        except Exception:
            logger.exception("babyg_agent_loop.run_cycle_crashed user=%s", creator_id)
            out.append({"user_id": creator_id, "status": "failed"})
    return out


def _active_creator_ids(*, limit: int) -> list[str]:
    """Every creator whose onboarding_completed_at is set. A creator
    who hasn't finished onboarding has no meaningful world state yet;
    running the loop for them is wasted tokens."""
    capped = max(1, min(int(limit), 1000))
    try:
        result = (
            supabase_client.get_service_client()
            .table("creator_profiles")
            .select("id")
            .not_.is_("onboarding_completed_at", None)
            .limit(capped)
            .execute()
        )
    except Exception:
        logger.exception("babyg_agent_loop.active_creator_ids.read_failed")
        return []
    rows = list(getattr(result, "data", None) or [])
    return [str(r.get("id")) for r in rows if r.get("id")]


def _record_and_return(
    user_id: str,
    *,
    status: str,
    started: datetime,
    delta: dict[str, int] | None = None,
    tools_called: list[dict[str, Any]] | None = None,
    final_response: str | None = None,
    system_prompt_hash: str | None = None,
    model: str | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost_usd: float = 0.0,
    skip_reason: str | None = None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> dict[str, Any]:
    ended = datetime.now(UTC)
    agent_cycles.record_cycle(
        user_id,
        status=status,
        cycle_started_at=started,
        cycle_ended_at=ended,
        delta=delta or {},
        tools_called=tools_called or [],
        final_response=final_response,
        system_prompt_hash=system_prompt_hash,
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost_usd,
        skip_reason=skip_reason,
        error_class=error_class,
        error_message=error_message,
    )
    return {
        "user_id": user_id,
        "status": status,
        "delta": delta or {},
        "tools_called": tools_called or [],
        "cost_usd": cost_usd,
    }
