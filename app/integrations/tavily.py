"""Tavily web search client.

Used by the babyg agent loop as a read-only tool when the creator
asks about something current babyg's local context can't answer
(public events, brand news, venue openings, current pricing, etc.).

Failure modes are explicit so the bot can route around them:
  - `TavilyNotConfiguredError`: TAVILY_API_KEY is empty.
  - `TavilyCallError`: configured, but the HTTP call failed.
Both surface from `bot._execute_read_tool` as a structured
{"available": False, "reason": "..."} tool result so Claude can
continue the turn without inventing facts.

Never log the API key or full response payloads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)

API_URL = "https://api.tavily.com/search"
DEFAULT_MAX_RESULTS = 5
HARD_MAX_RESULTS = 10
TIMEOUT_SECONDS = 12.0


class TavilyNotConfiguredError(RuntimeError):
    """Raised when search is requested but TAVILY_API_KEY is empty."""


class TavilyCallError(RuntimeError):
    """Raised for non-secret HTTP / parse failures against Tavily."""


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str
    published: str | None


def is_configured() -> bool:
    return bool(get_settings().tavily_api_key)


def search(query: str, *, max_results: int = DEFAULT_MAX_RESULTS) -> list[SearchHit]:
    """Run a Tavily search. Returns a compact list of hits.

    `max_results` is clamped to `HARD_MAX_RESULTS` so a runaway tool
    call can't pull a huge response. Tavily's `basic` search depth is
    cheap and fast; `advanced` costs a multiple but isn't needed for
    babyg's "what's happening" questions.
    """
    settings = get_settings()
    if not settings.tavily_api_key:
        raise TavilyNotConfiguredError("TAVILY_API_KEY is not set")
    if not (query or "").strip():
        return []
    payload = {
        "api_key": settings.tavily_api_key,
        "query": query.strip()[:400],
        "search_depth": "basic",
        "max_results": max(1, min(int(max_results or DEFAULT_MAX_RESULTS), HARD_MAX_RESULTS)),
        "include_answer": False,
        "include_raw_content": False,
        "include_images": False,
    }
    try:
        response = httpx.post(API_URL, json=payload, timeout=TIMEOUT_SECONDS)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        status = getattr(getattr(exc, "response", None), "status_code", "?")
        # Status only. Never log query bodies or API key.
        logger.info("Tavily search failed with status %s", status)
        raise TavilyCallError("Tavily search request failed") from exc
    try:
        data = response.json()
    except ValueError as exc:
        logger.info("Tavily search returned non-JSON")
        raise TavilyCallError("Tavily response was not JSON") from exc
    return _parse_hits(data)


def _parse_hits(data: Any) -> list[SearchHit]:
    if not isinstance(data, dict):
        return []
    raw = data.get("results")
    if not isinstance(raw, list):
        return []
    out: list[SearchHit] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        out.append(
            SearchHit(
                title=str(row.get("title") or "").strip()[:240],
                url=url[:600],
                snippet=str(row.get("content") or "").strip()[:600],
                published=(str(row.get("published_date")).strip() or None)
                if row.get("published_date")
                else None,
            )
        )
    return out
