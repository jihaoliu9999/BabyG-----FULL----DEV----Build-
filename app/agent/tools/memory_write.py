"""Internal-memory writes from the babyg agent loop.

Phase 9 tool: `remember`. Writes to babyg's own memory tables only. No
Gmail, no calendar, no DMs, no external side effects — ever. External
writes still route through action proposals; this module deliberately
does not import gmail / calendar / dms modules so a prompt-injection
attempt cannot escalate a `remember` call into a message send.

Kinds allowed (subset of babyg_memory._KIND_TABLE):

    decisions              structured record of a call the creator made
    creator_preferences    "no nightlife deals", "prefers fri/sat shoots"
    voice_samples          writing samples for tone matching
    relationship_notes     what babyg knows about how a brand behaves
    contract_flags         flagged clauses (usually written by the
                           contract-parse job, but the model can add one)

The intersection excludes: drafts, deals, deal_touchpoints — those go
through the deal/draft flow so stage transitions and touchpoint
threading stay in charge, not the model.
"""

from __future__ import annotations

import logging
from typing import Any

from app.services import babyg_memory, babyg_relations

logger = logging.getLogger(__name__)

_ALLOWED_KINDS: frozenset[str] = frozenset(
    {
        "decisions",
        "creator_preferences",
        "voice_samples",
        "relationship_notes",
        "contract_flags",
    }
)


def remember(user_id: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Write one memory row. Returns a small result envelope the bot
    loop feeds back to the model. Never raises."""
    kind = str(tool_input.get("kind") or "").strip()
    summary = str(tool_input.get("summary") or "").strip()
    brand_name = str(tool_input.get("brand_name") or "").strip() or None
    note_kind = str(tool_input.get("note_kind") or "").strip() or None

    if kind not in _ALLOWED_KINDS:
        return {
            "ok": False,
            "reason": (
                f"remember refused: kind must be one of "
                f"{sorted(_ALLOWED_KINDS)}. writes to drafts / deals / "
                f"touchpoints route through the deal flow, not this tool."
            ),
        }
    if not summary:
        return {"ok": False, "reason": "remember refused: summary is empty"}

    # Relationship notes get their own helper so brand_name / peer_id
    # scoping is enforced consistently with save_relationship_note.
    if kind == "relationship_notes":
        if not brand_name:
            return {
                "ok": False,
                "reason": (
                    "remember refused: relationship_notes needs a "
                    "brand_name so the note is retrievable later."
                ),
            }
        row = babyg_relations.save_relationship_note(
            user_id,
            kind=note_kind or "other",
            body=summary,
            brand_name=brand_name,
            babyg_source="remember_tool",
        )
        if row is None:
            return {"ok": False, "reason": "note write failed"}
        return {"ok": True, "kind": kind, "id": str(row.get("id") or "")}

    payload = _payload_for_kind(kind, summary=summary, brand_name=brand_name)
    row = babyg_memory.save(kind, user_id, payload)  # type: ignore[arg-type]
    if row is None:
        return {"ok": False, "reason": f"{kind} write failed"}
    return {"ok": True, "kind": kind, "id": str(row.get("id") or "")}


def _payload_for_kind(
    kind: str, *, summary: str, brand_name: str | None
) -> dict[str, Any]:
    """Shape the row body for each supported kind so callers do not
    need to know the migration column names."""
    if kind == "decisions":
        return {"kind": "note", "summary": summary}
    if kind == "creator_preferences":
        return {"summary": summary}
    if kind == "voice_samples":
        return {"sample": summary, "channel": "chat"}
    if kind == "contract_flags":
        return {"clause_type": "note", "summary": summary}
    # Shouldn't reach here; the caller already validated kind.
    return {"summary": summary}
