"""Lock the Google-Calendar upsert conflict-target contract.

Incident: migration 0041 replaced the two-column unique constraint
``unique (user_id, google_event_id)`` with a **partial** unique
index (``WHERE google_event_id is not null``) on the three
columns the upsert now targets. PostgreSQL cannot use a partial
index as the arbiter of a plain ``ON CONFLICT (columns)`` upsert
unless the ``WHERE`` predicate is repeated on the ``ON CONFLICT``
clause — and PostgREST does not expose that hook. Every synced
Google event failed to persist.

Migration 0043 replaces the partial index with a full unique
index on the same three columns; PostgREST's on_conflict
directive can now use it as the arbiter. This test file locks
that contract at three levels:

  1. ``bookings.upsert_google_event`` uses the exact three-column
     conflict target the schema expects.
  2. The latest schema migration (0043) creates a FULL unique
     index (no ``WHERE`` predicate) on those three columns.
  3. The pre-0043 partial-index migration (0041) is superseded:
     0043 explicitly drops the partial variant before creating
     the full one.

Also confirms the callers of ``upsert_google_event`` /
``cancel_google_event`` continue to pass the correct arguments so
multi-user + multi-calendar isolation is preserved.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.services import bookings as bookings_module

REPO = Path(__file__).resolve().parents[1]
BOOKINGS_SRC = REPO / "app" / "services" / "bookings.py"
MIG_0041 = REPO / "migrations" / "0041_calendar_google_metadata.sql"
MIG_0043 = REPO / "migrations" / "0043_bookings_google_unique_full.sql"


# ---------------------------------------------------------------------------
# 1. The application-side conflict target is exactly what the schema
#    provides.
# ---------------------------------------------------------------------------


def test_upsert_google_event_uses_three_column_conflict_target():
    """Static lock: ``upsert_google_event`` calls PostgREST's ``.upsert``
    with ``on_conflict='user_id,google_calendar_id,google_event_id'``.
    A future edit that swaps this back to a two-column target would
    reintroduce the incident."""
    src = BOOKINGS_SRC.read_text()
    assert (
        'on_conflict="user_id,google_calendar_id,google_event_id"' in src
    ), (
        "bookings.upsert_google_event must upsert with "
        "on_conflict='user_id,google_calendar_id,google_event_id' — this "
        "is the contract migration 0043 satisfies"
    )


# ---------------------------------------------------------------------------
# 2. Migration 0043 creates a FULL unique index (no WHERE predicate)
#    on the three columns.
# ---------------------------------------------------------------------------


def test_migration_0043_creates_full_unique_index_on_three_columns():
    """The migration that repairs the incident must:
      * exist,
      * drop the partial index the 0041 migration created,
      * create a full unique index (no WHERE clause) on the three
        columns the upsert targets.
    """
    assert MIG_0043.exists(), (
        "migration 0043 that repairs the Google Calendar upsert incident "
        "must exist"
    )
    sql = MIG_0043.read_text()

    # Normalize whitespace so the regexes below don't care about line
    # breaks or extra spaces.
    normalized = " ".join(sql.split())

    # Drops the partial variant first.
    assert re.search(
        r"drop\s+index\s+if\s+exists\s+public\.bookings_user_google_calendar_event_uidx",
        normalized,
        re.IGNORECASE,
    ), "0043 must drop the partial index before recreating it"

    # Creates a full unique index (no WHERE clause) on the three cols.
    # Anchor on the CREATE ... ON bookings(cols) shape; the constraint
    # is that no WHERE follows the column list.
    create_match = re.search(
        r"create\s+unique\s+index(?:\s+if\s+not\s+exists)?\s+"
        r"(?:public\.)?bookings_user_google_calendar_event_uidx\s+"
        r"on\s+public\.bookings\s*\(\s*user_id\s*,\s*google_calendar_id\s*,\s*google_event_id\s*\)"
        r"([^;]*);",
        normalized,
        re.IGNORECASE,
    )
    assert create_match, (
        "0043 must create a unique index on "
        "(user_id, google_calendar_id, google_event_id) — the columns the "
        "upsert on_conflict targets"
    )
    trailing = create_match.group(1).strip()
    assert "where" not in trailing.lower(), (
        "0043's unique index must be FULL (no WHERE predicate). A partial "
        "index does not satisfy PostgreSQL's ON CONFLICT arbiter "
        "requirement without a matching WHERE on the upsert, which "
        "PostgREST does not expose. Found trailing SQL: "
        f"{trailing!r}"
    )


# ---------------------------------------------------------------------------
# 3. Migration 0041's partial-index shape is preserved in history so
#    the diff between 0041 and 0043 stays legible.
# ---------------------------------------------------------------------------


def test_migration_0041_still_contains_original_partial_shape():
    """Sanity: 0041's file is unchanged. The repair is layered on top
    via 0043; we do NOT edit history."""
    assert MIG_0041.exists()
    sql = MIG_0041.read_text()
    normalized = " ".join(sql.split())
    assert (
        "where google_event_id is not null" in normalized.lower()
    ), (
        "0041 must retain its original partial index definition. "
        "Migration history is append-only; 0043 supersedes it."
    )


# ---------------------------------------------------------------------------
# 4. The application-side signatures preserve multi-user + multi-calendar
#    isolation.
# ---------------------------------------------------------------------------


def test_upsert_google_event_still_stamps_user_id():
    """A regression guard: the upsert must always attach ``user_id``
    into the body it sends to Supabase (``body = {**payload, "user_id":
    user_id}``). Otherwise multi-user isolation is broken."""
    src = BOOKINGS_SRC.read_text()
    assert 'body = {**payload, "user_id": user_id}' in src, (
        "upsert_google_event must always stamp user_id into the row body"
    )


def test_cancel_google_event_scopes_by_user_and_calendar():
    """The cancellation path must filter by (user_id,
    google_calendar_id, google_event_id) so one user's cancellation
    cannot flip another user's booking."""
    src = BOOKINGS_SRC.read_text()
    # We assert on the three chained ``.eq(...)`` calls the current
    # implementation uses.
    for eq_clause in (
        '.eq("user_id", user_id)',
        '.eq("google_calendar_id", google_calendar_id or "primary")',
        '.eq("google_event_id", google_event_id)',
    ):
        assert eq_clause in src, (
            f"cancel_google_event must scope its update by {eq_clause!r} "
            "so cross-user or cross-calendar cancellations are impossible"
        )


# ---------------------------------------------------------------------------
# 5. upsert_google_event still returns True on a successful response
#    and False on PostgrestAPIError — the failure path was masking the
#    incident silently. That semantic must not regress.
# ---------------------------------------------------------------------------


def test_upsert_google_event_returns_false_on_postgrest_error(monkeypatch):
    """Simulate a PostgrestAPIError bubbling out of ``.execute()`` and
    verify the function returns False rather than raising. This is the
    exact code path the incident took — every event returned False,
    which the caller counts as ``skipped``."""
    from postgrest.exceptions import APIError as PostgrestAPIError

    class _ExecStub:
        def execute(self):
            raise PostgrestAPIError({"message": "simulated conflict-target error"})

    class _UpsertStub:
        def upsert(self, body, on_conflict):
            return _ExecStub()

    class _TableStub:
        def table(self, name):
            assert name == "bookings"
            return _UpsertStub()

    monkeypatch.setattr(
        bookings_module.supabase_client, "get_service_client", lambda: _TableStub()
    )
    result = bookings_module.upsert_google_event(
        user_id="u-1",
        payload={
            "title": "campaign call",
            "starts_at": "2026-09-24T14:00:00+00:00",
            "google_calendar_id": "primary",
            "google_event_id": "evt-1",
            "type": "event",
        },
    )
    assert result is False


def test_upsert_google_event_returns_true_on_success(monkeypatch):
    class _ExecStub:
        def execute(self):
            class _R:
                def __init__(self):
                    self.data = [{"id": "b-1"}]
            return _R()

    class _UpsertStub:
        def upsert(self, body, on_conflict):
            # Contract lock: the exact conflict target the schema
            # supports must be the one the upsert sends.
            assert on_conflict == "user_id,google_calendar_id,google_event_id", (
                f"unexpected on_conflict: {on_conflict!r}"
            )
            return _ExecStub()

    class _TableStub:
        def table(self, name):
            assert name == "bookings"
            return _UpsertStub()

    monkeypatch.setattr(
        bookings_module.supabase_client, "get_service_client", lambda: _TableStub()
    )
    result = bookings_module.upsert_google_event(
        user_id="u-1",
        payload={
            "title": "campaign call",
            "starts_at": "2026-09-24T14:00:00+00:00",
            "google_calendar_id": "primary",
            "google_event_id": "evt-1",
            "type": "event",
        },
    )
    assert result is True


# ---------------------------------------------------------------------------
# 6. Missing google_event_id short-circuits — no attempted upsert.
#    (Guards against a future edit that dropped this guard and started
#    upserting rows with NULL google_event_id, which under the new full
#    unique index would still be safe but would silently create
#    duplicates.)
# ---------------------------------------------------------------------------


def test_upsert_google_event_refuses_empty_google_event_id(monkeypatch):
    called: list[str] = []

    class _Refuse:
        def table(self, name):
            called.append(name)
            raise AssertionError(
                "upsert_google_event must NOT reach Supabase when "
                "google_event_id is empty"
            )

    monkeypatch.setattr(
        bookings_module.supabase_client, "get_service_client", lambda: _Refuse()
    )
    result = bookings_module.upsert_google_event(
        user_id="u-1",
        payload={"title": "no id", "google_event_id": ""},
    )
    assert result is False
    assert called == []
