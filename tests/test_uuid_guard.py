"""Tests for `safe_uuid` — defense boundary for raw postgrest filter
strings.

Several services interpolate IDs into `.or_(f"col.eq.{value}, ...")`
predicates. `safe_uuid` is the gate: it MUST return either a valid,
canonicalized UUID string or the empty string. Returning anything that
parses as filter syntax (commas, parens, dots) would re-open the
filter-injection class.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from app.core.uuid_guard import safe_uuid

# ---------- accepted ----------


def test_accepts_canonical_uuid():
    u = str(uuid4())
    assert safe_uuid(u) == u


def test_accepts_uppercase_uuid_and_canonicalizes_to_lowercase():
    """The Python `UUID` constructor accepts mixed case; `str(UUID(...))`
    normalises to lowercase. Postgres comparisons are case-sensitive on
    text but uuids parse case-insensitively, so canonicalising here
    also prevents a bypass where two casings represent the same value."""
    upper = str(uuid4()).upper()
    out = safe_uuid(upper)
    assert out == upper.lower()
    # Round-trip: the result must still parse as a UUID.
    assert UUID(out)


def test_accepts_uuid_with_braces_and_strips_them():
    raw = str(uuid4())
    assert safe_uuid(f"{{{raw}}}") == raw


def test_accepts_uuid_without_hyphens_and_canonicalizes():
    raw = str(uuid4())
    no_hyphens = raw.replace("-", "")
    assert safe_uuid(no_hyphens) == raw


# ---------- rejected (the dangerous shapes) ----------


@pytest.mark.parametrize(
    "value",
    [
        "",
        "not-a-uuid",
        "00000000-0000-0000-0000-00000000000",   # one char short
        "00000000-0000-0000-0000-0000000000000", # one char long
        "aaa),or(status.eq.accepted",            # postgrest filter injection
        "*",
        "1' OR '1'='1",
        "00000000-0000-0000-0000-000000000000)", # trailing paren
        "abcdef.eq.foo",                         # postgrest filter syntax
        "abc,def",                               # comma inside
        "abc.def",                               # dot inside
        "00000000-0000-0000-0000-000000000000," "extra",  # canonical + suffix
    ],
)
def test_rejects_filter_injection_shapes(value: str):
    out = safe_uuid(value)
    # MUST be either empty or a clean canonical UUID — never echo the
    # attacker-controlled input.
    assert out == "" or UUID(out)
    # And no filter syntax characters in the returned value.
    for forbidden in (",", "(", ")", "*", " ", "'"):
        assert forbidden not in out


def test_rejects_non_string_input():
    for v in (None, 123, ["00000000-0000-0000-0000-000000000000"], object(), b"x"):
        assert safe_uuid(v) == ""  # type: ignore[arg-type]


def test_returned_value_is_filter_safe_for_canonical_input():
    """Final smoke: anything safe_uuid returns can be interpolated into
    `f"col.eq.{value}"` without breaking out of the term. The character
    set is `[0-9a-f-]`."""
    out = safe_uuid(str(uuid4()))
    assert out
    assert all(ch in "0123456789abcdef-" for ch in out)
