"""Per-creator daily cost cap for the babyg background agent.

Every claude call the agent loop makes gets weighed against a
per-creator daily budget. When today's dollar spend crosses the
cap, the next cycle short-circuits (no LLM call) until midnight UTC.
The next day resets the ledger — a new row is upserted on first
write.

Public shape:

    today_spend(user_id)                    -> dict
    over_daily_cap(user_id)                 -> bool
    record_cycle(user_id, ..., cost_usd)    -> dict | None
    estimate_cost_usd(prompt_tokens, completion_tokens, model)
                                            -> float

The DAILY_CAP_USD constant defaults to $0.10 per the "tight" bracket
the creator picked when we designed the agent. Environments can
override with BABYG_AGENT_DAILY_CAP_USD.

Pricing lives in _MODEL_PRICES. Add a row when adopting a new model;
missing model logs a WARNING and estimates against the default
(Haiku), which is intentionally conservative — an unknown model
should not silently be assumed cheap.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime
from typing import Any

from app.core import supabase_client

logger = logging.getLogger(__name__)

# Default per-creator per-day cap. Overridable per-env; tests
# typically leave the default in place.
DEFAULT_DAILY_CAP_USD = 0.10


def daily_cap_usd() -> float:
    """Return the effective cap. Reads env each call so a test can
    monkeypatch os.environ without touching this module."""
    raw = os.environ.get("BABYG_AGENT_DAILY_CAP_USD")
    if raw is None or not raw.strip():
        return DEFAULT_DAILY_CAP_USD
    try:
        value = float(raw)
    except ValueError:
        logger.warning("agent_cost.bad_cap_env value=%s", raw)
        return DEFAULT_DAILY_CAP_USD
    return max(value, 0.0)


# Blended prices per 1M tokens, USD. Blended is fine here: we track
# input + output separately so the exact per-token blend is captured
# in cost_usd; this table is only for estimate_cost_usd().
#
# Keep this list short and current. When an unknown model is passed
# in, we fall back to the DEFAULT_MODEL row (Haiku), which is the
# conservative choice.
_MODEL_PRICES: dict[str, tuple[float, float]] = {
    # model            (input $/1M, output $/1M)
    "claude-haiku-4-5-20251001": (1.00, 5.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-sonnet-4-20250514": (3.00, 15.00),
    "claude-sonnet-4-5": (3.00, 15.00),
    "claude-opus-4-7": (15.00, 75.00),
}
_DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def estimate_cost_usd(
    prompt_tokens: int, completion_tokens: int, model: str
) -> float:
    """Rough dollar cost for one claude call, used pre-flight for
    'would this cycle put me over budget?' checks. record_cycle
    accepts an authoritative cost_usd separately, so this doesn't
    need to be exact — just conservative."""
    prices = _MODEL_PRICES.get(model)
    if prices is None:
        logger.warning(
            "agent_cost.unknown_model model=%s fallback=%s", model, _DEFAULT_MODEL
        )
        prices = _MODEL_PRICES[_DEFAULT_MODEL]
    input_price_per_1m, output_price_per_1m = prices
    prompt_cost = (prompt_tokens / 1_000_000.0) * input_price_per_1m
    completion_cost = (completion_tokens / 1_000_000.0) * output_price_per_1m
    return round(prompt_cost + completion_cost, 6)


def _today_utc() -> date:
    return datetime.now(UTC).date()


def today_spend(user_id: str, *, today: date | None = None) -> dict[str, Any]:
    """Return today's rollup row for this creator, or zeros if none exists."""
    day = (today or _today_utc()).isoformat()
    try:
        result = (
            supabase_client.get_service_client()
            .table("agent_daily_spend")
            .select("prompt_tokens,completion_tokens,cost_usd,cycles_run,last_cycle_at")
            .eq("user_id", user_id)
            .eq("day", day)
            .limit(1)
            .execute()
        )
    except Exception:
        logger.exception("agent_cost.today_spend.read_failed user=%s", user_id)
        return _empty_row(day)
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return _empty_row(day)
    row = rows[0]
    return {
        "day": day,
        "prompt_tokens": int(row.get("prompt_tokens") or 0),
        "completion_tokens": int(row.get("completion_tokens") or 0),
        "cost_usd": float(row.get("cost_usd") or 0.0),
        "cycles_run": int(row.get("cycles_run") or 0),
        "last_cycle_at": row.get("last_cycle_at"),
    }


def over_daily_cap(user_id: str, *, today: date | None = None) -> bool:
    """True when today's cost has met or exceeded the cap. Used by the
    agent loop to short-circuit before spending a token."""
    row = today_spend(user_id, today=today)
    cap = daily_cap_usd()
    if cap <= 0:
        # A cap of 0 means the agent is disabled. Treat as always-over.
        return True
    return row["cost_usd"] >= cap


def record_cycle(
    user_id: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float,
    today: date | None = None,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Upsert today's row for this creator, incrementing counters by
    this cycle's usage. Returns the resulting row or None on failure.

    Cycle counts and tokens accumulate additively; cost_usd is the
    caller-supplied dollar amount (usually estimate_cost_usd applied
    to the actual token counts the SDK returns).
    """
    day = (today or _today_utc()).isoformat()
    ts = (now or datetime.now(UTC)).isoformat()
    existing = today_spend(user_id, today=today or _today_utc())
    payload = {
        "user_id": user_id,
        "day": day,
        "prompt_tokens": existing["prompt_tokens"] + max(int(prompt_tokens), 0),
        "completion_tokens": existing["completion_tokens"]
        + max(int(completion_tokens), 0),
        "cost_usd": round(existing["cost_usd"] + max(float(cost_usd), 0.0), 6),
        "cycles_run": existing["cycles_run"] + 1,
        "last_cycle_at": ts,
    }
    try:
        result = (
            supabase_client.get_service_client()
            .table("agent_daily_spend")
            .upsert(payload, on_conflict="user_id,day")
            .execute()
        )
    except Exception:
        logger.exception("agent_cost.record_cycle.write_failed user=%s", user_id)
        return None
    rows = list(getattr(result, "data", None) or [])
    return rows[0] if rows else payload


def _empty_row(day: str) -> dict[str, Any]:
    return {
        "day": day,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "cost_usd": 0.0,
        "cycles_run": 0,
        "last_cycle_at": None,
    }
