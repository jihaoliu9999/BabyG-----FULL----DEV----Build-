"""Manager deal-stage awareness (babyg_awareness._open_deal_stage).

The reader used to filter dm_ai_briefs on ``recipient_id`` — a column the
table has never had (migrations 0022/0026 define ``recipient_user_id``).
PostgREST rejects a filter on an unknown column, the reader swallowed the
error, and the Manager silently never saw a deal stage. These tests run the
reader against a stub that knows dm_ai_briefs' real columns (parsed from
the migrations) and rejects unknown ones the way PostgREST does, then check
the data the Manager actually receives.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from postgrest.exceptions import APIError

from app.core import supabase_client
from app.services import babyg_awareness, bot

ROOT = Path(__file__).parents[1]
USER_A = "11111111-1111-4111-8111-111111111111"
USER_B = "22222222-2222-4222-8222-222222222222"
THREAD_AB = "33333333-3333-4333-8333-333333333333"


def _dm_ai_briefs_columns() -> set[str]:
    """Columns of public.dm_ai_briefs as the migrations define them."""
    create = (ROOT / "migrations/0022_dm_ai_briefs.sql").read_text(encoding="utf-8")
    body = create.split("create table if not exists public.dm_ai_briefs (", 1)[1].split("\n);", 1)[0]
    columns = {
        m.group(1)
        for m in re.finditer(r"^\s{2}([a-z_]+)\s+[a-z]", body, flags=re.M)
        if m.group(1) not in {"check", "constraint", "unique", "primary"}
    }
    upgrade = (ROOT / "migrations/0026_dm_ai_brief_upgrade.sql").read_text(encoding="utf-8")
    columns |= set(re.findall(r"add column if not exists ([a-z_]+)", upgrade))
    return columns


COLUMNS = _dm_ai_briefs_columns()


class _Query:
    """PostgREST-shaped query over dm_ai_briefs rows. Referencing a column
    the table doesn't have fails at execute(), as the real API does."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows
        self._unknown: str | None = None

    def _check(self, col: str) -> None:
        if col not in COLUMNS and self._unknown is None:
            self._unknown = col

    def select(self, cols: str = "*", **_k: Any) -> _Query:
        for col in (c.strip() for c in cols.split(",")):
            if col != "*":
                self._check(col)
        return self

    def eq(self, col: str, val: Any) -> _Query:
        self._check(col)
        self._rows = [r for r in self._rows if r.get(col) == val]
        return self

    @property
    def not_(self) -> SimpleNamespace:
        def is_(col: str, val: str) -> _Query:
            self._check(col)
            if val == "null":
                self._rows = [r for r in self._rows if r.get(col) is not None]
            return self

        return SimpleNamespace(is_=is_)

    def limit(self, n: int) -> _Query:
        self._rows = self._rows[:n]
        return self

    def execute(self) -> SimpleNamespace:
        if self._unknown is not None:
            raise APIError({
                "code": "42703",
                "message": f"column dm_ai_briefs.{self._unknown} does not exist",
            })
        return SimpleNamespace(data=list(self._rows))


class _Client:
    def __init__(self, briefs: list[dict[str, Any]]) -> None:
        self.briefs = briefs

    def table(self, name: str) -> Any:
        if name != "dm_ai_briefs":
            raise RuntimeError(f"no other table in this stub: {name}")
        return _Query([dict(r) for r in self.briefs])


def _brief(recipient: str, stage: str | None, **extra: Any) -> dict[str, Any]:
    row = {"id": f"brief-{recipient[:4]}-{stage}", "thread_id": THREAD_AB,
           "recipient_user_id": recipient, "deal_stage": stage}
    row.update(extra)
    return row


@pytest.fixture()
def briefs(monkeypatch):
    rows: list[dict[str, Any]] = []
    monkeypatch.setattr(supabase_client, "get_service_client", lambda: _Client(rows))
    babyg_awareness._CACHE.clear()
    yield rows
    babyg_awareness._CACHE.clear()


def test_migrations_define_recipient_user_id_not_recipient_id() -> None:
    assert {"recipient_user_id", "deal_stage", "thread_id", "message_id"} <= COLUMNS
    assert "recipient_id" not in COLUMNS


def test_deal_stage_is_read_from_the_recipients_briefs(briefs) -> None:
    """The bug: this returned None because the query filtered a column
    that doesn't exist and the error was swallowed."""
    briefs.append(_brief(USER_A, "negotiating"))
    assert babyg_awareness._open_deal_stage(USER_A) == "negotiating"


def test_highest_priority_open_stage_wins(briefs) -> None:
    briefs.extend([
        _brief(USER_A, "new_inquiry"),
        _brief(USER_A, "scheduled"),
        _brief(USER_A, "waiting_terms"),
        _brief(USER_A, "negotiating"),
    ])
    assert babyg_awareness._open_deal_stage(USER_A) == "waiting_terms"


@pytest.mark.parametrize("stages", [[], [None], ["accepted"], ["declined"], ["accepted", None, "declined"]])
def test_no_signal_without_an_open_stage(briefs, stages) -> None:
    briefs.extend(_brief(USER_A, s) for s in stages)
    assert babyg_awareness._open_deal_stage(USER_A) is None


def test_another_users_briefs_never_reach_this_manager(briefs) -> None:
    briefs.append(_brief(USER_B, "negotiating"))
    assert babyg_awareness._open_deal_stage(USER_A) is None
    briefs.append(_brief(USER_A, "qualifying"))
    assert babyg_awareness._open_deal_stage(USER_A) == "qualifying"
    assert babyg_awareness._open_deal_stage(USER_B) == "negotiating"


def test_sender_never_sees_the_recipients_private_brief(briefs) -> None:
    """A messaged B in a shared thread; B's brief on that message is
    recipient-private (RLS: recipient_user_id = auth.uid()). It is B's
    signal, never A's."""
    briefs.append(_brief(USER_B, "waiting_terms", sender_context={"sender_user_id": USER_A}))
    assert babyg_awareness._open_deal_stage(USER_A) is None
    assert babyg_awareness._open_deal_stage(USER_B) == "waiting_terms"


def test_read_failure_still_degrades_to_no_signal(monkeypatch) -> None:
    def broken() -> Any:
        raise RuntimeError("supabase down")

    monkeypatch.setattr(supabase_client, "get_service_client", broken)
    assert babyg_awareness._open_deal_stage(USER_A) is None


def test_deal_stage_reaches_the_manager_snapshot_and_prompt_state(briefs, monkeypatch) -> None:
    """End to end: snapshot -> summary lines -> the "state" block the
    Manager's system prompt carries on every turn. Other readers degrade
    to empty against this stub, which only knows dm_ai_briefs."""
    briefs.append(_brief(USER_A, "negotiating"))

    snap = babyg_awareness.snapshot(USER_A, force=True)
    assert snap["open_deal_stage"] == "negotiating"
    assert "- an open deal is at stage: negotiating" in babyg_awareness.snapshot_summary_lines(snap)

    ctx = bot._build_prompt_context(user_id=USER_A, user_now_iso=None, user_tz=None)
    assert "an open deal is at stage: negotiating" in ctx.get("state", "")


def test_no_deal_line_in_the_prompt_when_there_is_no_open_stage(briefs) -> None:
    briefs.append(_brief(USER_B, "negotiating"))
    snap = babyg_awareness.snapshot(USER_A, force=True)
    assert snap["open_deal_stage"] is None
    assert not any("open deal" in line for line in babyg_awareness.snapshot_summary_lines(snap))
