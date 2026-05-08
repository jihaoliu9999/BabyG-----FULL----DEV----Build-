"""UUID sanitizer for postgrest filter strings.

Several services build `.or_(f"col.eq.{value},...")` filters with f-string
interpolation. Today every value comes from a validated DB lookup or a
signed session cookie, so it's safe — but the pattern is fragile. Any
future caller that forwards a raw URL path arg here would inject
postgrest filter syntax (commas, dots, parens). `safe_uuid` is a defense
boundary: callers pass values through it before interpolation, and
non-UUIDs become the empty string (which never matches a real row).
"""

from __future__ import annotations

from uuid import UUID


def safe_uuid(value: str) -> str:
    """Return `value` if it parses as a UUID, else "" (which won't match)."""
    if not isinstance(value, str):
        return ""
    try:
        return str(UUID(value))
    except (ValueError, AttributeError, TypeError):
        return ""
