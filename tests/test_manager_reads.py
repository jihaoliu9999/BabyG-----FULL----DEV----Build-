"""Manager load read optimisation (manager_reads).

Locks in that opening /creator/bot:
  * reads the discover feed, upcoming bookings and the creator profile
    once instead of two or three times,
  * runs independent reads concurrently,
  * hands every reader the same data it read before (own copies),
  * never shares a read across requests, users or scopes,
  * keeps each reader's failure behavior.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import Response
from fastapi.testclient import TestClient

from app.core.security import SESSION_COOKIE, write_session
from app.routes import creator as creator_routes
from app.services import babyg_awareness, bot_nudges, manager_reads
from app.services import bookings as bookings_module
from app.services import discover as discover_module
from app.services import profiles as profiles_module

_TIMEOUT = 5.0


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")


def _signed_in(client: TestClient, user_id: str = "u-1") -> None:
    resp = Response()
    write_session(resp, {"user_id": user_id, "role": "creator"})
    client.cookies.set(SESSION_COOKIE, resp.headers["set-cookie"].split(";")[0].split("=", 1)[1])


# ---------------------------------------------------------------------------
# shared(): one read per key per scope, own copies, no leakage
# ---------------------------------------------------------------------------


def test_shared_runs_an_identical_read_once_per_scope() -> None:
    calls: list[str] = []

    def read() -> list[dict[str, Any]]:
        calls.append("x")
        return [{"id": "c1"}]

    with manager_reads.scope():
        first = manager_reads.shared(("feed", "u-1"), read)
        second = manager_reads.shared(("feed", "u-1"), read)

    assert calls == ["x"]
    assert first == second == [{"id": "c1"}]


def test_shared_hands_each_caller_its_own_copy() -> None:
    with manager_reads.scope():
        first = manager_reads.shared(("feed", "u-1"), lambda: [{"id": "c1"}])
        first[0]["id"] = "mutated"
        first.append({"id": "extra"})
        second = manager_reads.shared(("feed", "u-1"), lambda: [{"id": "never"}])

    assert second == [{"id": "c1"}]


def test_shared_outside_a_scope_is_a_plain_read_every_time() -> None:
    calls: list[str] = []
    for _ in range(3):
        manager_reads.shared(("feed", "u-1"), lambda: calls.append("x"))
    assert calls == ["x", "x", "x"]


def test_nothing_is_shared_after_the_scope_closes() -> None:
    calls: list[str] = []
    with manager_reads.scope():
        manager_reads.shared(("feed", "u-1"), lambda: calls.append("in"))
    manager_reads.shared(("feed", "u-1"), lambda: calls.append("after"))
    with manager_reads.scope():
        manager_reads.shared(("feed", "u-1"), lambda: calls.append("next scope"))
    assert calls == ["in", "after", "next scope"]


def test_different_users_never_share_a_read() -> None:
    with manager_reads.scope():
        a = manager_reads.shared(manager_reads.creator_profile_key("u-1"), lambda: {"user_id": "u-1"})
        b = manager_reads.shared(manager_reads.creator_profile_key("u-2"), lambda: {"user_id": "u-2"})
    assert a == {"user_id": "u-1"}
    assert b == {"user_id": "u-2"}


def test_concurrent_requests_get_isolated_scopes() -> None:
    """Two overlapping Manager loads (one per thread, like two requests)
    seeded for the same key each see only their own value."""
    barrier = threading.Barrier(2, timeout=_TIMEOUT)
    seen: dict[str, Any] = {}

    def request(user: str) -> None:
        key = manager_reads.creator_profile_key("same-key")
        with manager_reads.scope(seed={key: {"owner": user}}):
            barrier.wait()  # both scopes are open at the same time
            seen[user] = manager_reads.shared(key, lambda: {"owner": "db"})
            barrier.wait()

    threads = [threading.Thread(target=request, args=(u,)) for u in ("alice", "bob")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(_TIMEOUT)
    assert seen == {"alice": {"owner": "alice"}, "bob": {"owner": "bob"}}


def test_failed_shared_read_is_not_reused() -> None:
    """A failing shared read raises for its caller (who handles it as
    before) and the next caller makes its own attempt."""
    attempts: list[str] = []

    def flaky() -> list[str]:
        attempts.append("try")
        if len(attempts) == 1:
            raise RuntimeError("blip")
        return ["ok"]

    with manager_reads.scope():
        with pytest.raises(RuntimeError):
            manager_reads.shared(("feed", "u-1"), flaky)
        assert manager_reads.shared(("feed", "u-1"), flaky) == ["ok"]
    assert attempts == ["try", "try"]


def test_waiting_caller_falls_back_to_its_own_read_when_the_shared_one_fails() -> None:
    started = threading.Event()
    release = threading.Event()
    results: dict[str, Any] = {}

    def failing_owner() -> list[str]:
        started.set()
        release.wait(_TIMEOUT)
        raise RuntimeError("owner failed")

    def owner() -> None:
        try:
            manager_reads.shared(("feed", "u-1"), failing_owner)
        except RuntimeError as exc:
            results["owner"] = str(exc)

    def waiter() -> None:
        started.wait(_TIMEOUT)
        threading.Timer(0.05, release.set).start()
        results["waiter"] = manager_reads.shared(("feed", "u-1"), lambda: ["own read"])

    with manager_reads.scope():
        manager_reads.run_parallel(owner, waiter)
    assert results == {"owner": "owner failed", "waiter": ["own read"]}


def test_upcoming_bookings_slices_one_shared_read(monkeypatch) -> None:
    rows = [{"id": f"b{i}", "starts_at": f"2026-10-{10 + i}T10:00:00+00:00"} for i in range(15)]
    calls: list[int] = []

    def fake_list(uid: str, *, horizon: str, limit: int) -> list[dict[str, Any]]:
        assert horizon == "upcoming"
        calls.append(limit)
        return rows[:limit]

    monkeypatch.setattr(bookings_module, "list_for_user", fake_list)
    with manager_reads.scope():
        six = manager_reads.upcoming_bookings("u-1", limit=6)
        twelve = manager_reads.upcoming_bookings("u-1", limit=12)
        twenty = manager_reads.upcoming_bookings("u-1", limit=20)
    assert calls == [manager_reads.UPCOMING_BOOKINGS_LIMIT]
    assert six == rows[:6] and twelve == rows[:12] and twenty == rows[:20]

    # Outside a scope every caller makes its own exact read.
    calls.clear()
    assert manager_reads.upcoming_bookings("u-1", limit=6) == rows[:6]
    assert calls == [6]


# ---------------------------------------------------------------------------
# run_parallel(): concurrency, order, failure, request context
# ---------------------------------------------------------------------------


def test_run_parallel_runs_calls_concurrently_and_keeps_order() -> None:
    barrier = threading.Barrier(3, timeout=_TIMEOUT)

    def call(value: str):
        def _run() -> str:
            barrier.wait()  # only passes if all three run at once
            return value

        return _run

    assert manager_reads.run_parallel(call("a"), call("b"), call("c")) == ["a", "b", "c"]


def test_run_parallel_raises_the_first_failure_in_call_order_after_all_ran() -> None:
    ran: list[str] = []

    def ok(name: str):
        return lambda: ran.append(name)

    def fail(name: str):
        def _run() -> None:
            ran.append(name)
            raise ValueError(name)

        return _run

    with pytest.raises(ValueError, match="second"):
        manager_reads.run_parallel(ok("first"), fail("second"), fail("third"), ok("fourth"))
    assert sorted(ran) == ["first", "fourth", "second", "third"]


def test_run_parallel_workers_see_the_callers_scope() -> None:
    calls: list[str] = []
    with manager_reads.scope():
        manager_reads.run_parallel(
            lambda: manager_reads.shared(("k", "u-1"), lambda: calls.append("read")),
            lambda: manager_reads.shared(("k", "u-1"), lambda: calls.append("read")),
        )
    assert calls == ["read"]


# ---------------------------------------------------------------------------
# awareness snapshot: readers run concurrently, same dict
# ---------------------------------------------------------------------------


_READERS = (
    "_unread_dms",
    "_recent_connection_accepted",
    "_recent_incoming_connection",
    "_next_booking",
    "_pending_booking",
    "_fresh_discover_match",
    "_pending_action_proposal",
    "_recent_hot_drop",
    "_open_deal_stage",
)


def test_snapshot_readers_run_concurrently_and_build_the_same_dict(monkeypatch) -> None:
    barrier = threading.Barrier(len(_READERS), timeout=_TIMEOUT)
    for name in _READERS:

        def reader(_uid: str, _name: str = name) -> str:
            barrier.wait()
            return f"value of {_name}"

        monkeypatch.setattr(babyg_awareness, name, reader)

    snap = babyg_awareness._build("u-1")
    # Same keys, same order, each filled by its own reader.
    assert list(snap) == list(babyg_awareness._empty())
    assert snap == {
        "unread_dms": "value of _unread_dms",
        "recent_connection_accepted": "value of _recent_connection_accepted",
        "recent_incoming_connection": "value of _recent_incoming_connection",
        "next_booking": "value of _next_booking",
        "pending_booking": "value of _pending_booking",
        "fresh_discover_match": "value of _fresh_discover_match",
        "pending_action_proposal": "value of _pending_action_proposal",
        "recent_hot_drop": "value of _recent_hot_drop",
        "open_deal_stage": "value of _open_deal_stage",
    }


def test_unread_dms_count_and_peer_lookup_run_concurrently(monkeypatch) -> None:
    from app.services import dms as dms_module

    barrier = threading.Barrier(2, timeout=_TIMEOUT)

    def count(uid: str) -> int:
        barrier.wait()
        return 4

    def threads(uid: str) -> list[dict[str, Any]]:
        barrier.wait()
        return [{"peer_id": "peer-1"}]

    monkeypatch.setattr(dms_module, "unread_count_for_user", count)
    monkeypatch.setattr(dms_module, "list_threads_for_user", threads)
    monkeypatch.setattr(
        profiles_module, "get_creators_by_ids", lambda ids: {"peer-1": {"full_name": "Ana Ruiz"}}
    )
    assert babyg_awareness._unread_dms("u-1") == {"count": 4, "latest_peer_name": "Ana Ruiz"}


# ---------------------------------------------------------------------------
# nudges: shared reads, same candidates, same insert order, same failures
# ---------------------------------------------------------------------------


@pytest.fixture()
def counted_reads(monkeypatch):
    now = datetime.now(UTC)
    calls: dict[str, list[Any]] = {"discover": [], "bookings": [], "profile": []}
    card = {
        "card_kind": "opportunity",
        "card_id": "op-1",
        "title": "Chobani UGC",
        "subtitle": "$850",
        "created_at": _iso(now - timedelta(hours=2)),
    }
    upcoming = [
        {"id": "bk-1", "title": "Olipop shoot", "status": "pending",
         "starts_at": _iso(now + timedelta(hours=6))},
        {"id": "bk-2", "title": "Later", "status": "confirmed",
         "starts_at": _iso(now + timedelta(days=3))},
    ]

    def list_cards(**kw: Any) -> list[dict[str, Any]]:
        calls["discover"].append(kw)
        return [dict(card)]

    def list_for_user(uid: str, **kw: Any) -> list[dict[str, Any]]:
        calls["bookings"].append(kw)
        return [dict(b) for b in upcoming][: kw.get("limit", 200)]

    def get_creator_profile(uid: str) -> dict[str, Any]:
        calls["profile"].append(uid)
        return {"user_id": uid, "onboarding_completed_at": "2026-01-01T00:00:00Z",
                "full_name": "Maya Lee", "niches": ["fashion"], "tier": "basic"}

    monkeypatch.setattr(discover_module, "list_cards", list_cards)
    monkeypatch.setattr(bookings_module, "list_for_user", list_for_user)
    monkeypatch.setattr(profiles_module, "get_creator_profile", get_creator_profile)
    return calls


@pytest.fixture()
def captured_inserts(monkeypatch):
    inserted: list[dict[str, Any]] = []

    def create(**body: Any) -> str:
        inserted.append(body)
        return f"m-{len(inserted)}"

    monkeypatch.setattr(bot_nudges, "create_message", create)
    monkeypatch.setattr(bot_nudges, "list_messages", lambda uid, limit=60: [])
    return inserted


def test_generate_pending_reads_discover_and_bookings_once(counted_reads, captured_inserts) -> None:
    """The nudge sources and the (cold) awareness snapshot used to read
    the discover feed twice and upcoming bookings three times (20/6/12)."""
    bot_nudges.generate_pending("u-1")

    assert len(counted_reads["discover"]) == 1
    assert counted_reads["discover"][0] == {
        "viewer_id": "u-1", "viewer_role": "creator", "kind": "all", "limit": 6,
    }
    assert [kw["limit"] for kw in counted_reads["bookings"]] == [20]
    assert all(kw["horizon"] == "upcoming" for kw in counted_reads["bookings"])

    # The snapshot built from the shared reads is what the separate
    # reads produced before.
    snap = babyg_awareness._CACHE["u-1"][1]
    assert snap["fresh_discover_match"] == {
        "card_id": "op-1", "card_kind": "opportunity", "title": "Chobani UGC", "subtitle": "$850",
    }
    assert snap["next_booking"]["id"] == "bk-1"
    assert snap["pending_booking"] == {
        "id": "bk-1", "title": "Olipop shoot", "starts_at": snap["pending_booking"]["starts_at"],
    }
    keys = [body["tool_calls"]["nudge_key"] for body in captured_inserts]
    assert keys[:2] == ["new_match:opportunity:op-1", "booking_pending:bk-1"]


def test_generate_pending_inserts_in_priority_order_whatever_finishes_first(
    monkeypatch, captured_inserts
) -> None:
    def source(key: str, delay: float):
        def _run(_uid: str) -> list[dict[str, Any]]:
            time.sleep(delay)
            return [{"nudge_key": key, "category": key, "content": key, "chips": []}]

        return _run

    # Earlier-priority sources finish last.
    order = [
        ("_match_nudges", "match"), ("_booking_nudges", "booking"),
        ("_connection_accepted_nudges", "connection"), ("_event_soon_nudges", "event"),
        ("_pending_action_nudges", "action"), ("_hot_drop_nudges", "hot"),
        ("_ghosted_deal_nudges", "ghosted"), ("_late_payment_nudges", "late"),
        ("_stale_draft_nudges", "draft"),
    ]
    for i, (name, key) in enumerate(order):
        monkeypatch.setattr(bot_nudges, name, source(key, 0.01 * (len(order) - i)))

    bot_nudges.generate_pending("u-1")
    assert [body["content"] for body in captured_inserts] == [key for _, key in order]


def test_generate_pending_sources_run_concurrently(monkeypatch, captured_inserts) -> None:
    barrier = threading.Barrier(6, timeout=_TIMEOUT)

    def source(_uid: str) -> list[dict[str, Any]]:
        barrier.wait()
        return []

    for name in ("_match_nudges", "_booking_nudges", "_ghosted_deal_nudges",
                 "_late_payment_nudges", "_stale_draft_nudges", "_snapshot_nudges"):
        monkeypatch.setattr(bot_nudges, name, source)
    assert bot_nudges.generate_pending("u-1") == []


def test_generate_pending_source_failure_still_inserts_nothing(monkeypatch, captured_inserts) -> None:
    """A source raising outside its own guard aborted the batch before any
    insert; it still does."""
    monkeypatch.setattr(
        bot_nudges, "_match_nudges",
        lambda _uid: [{"nudge_key": "k", "category": "c", "content": "x", "chips": []}],
    )

    def boom(_uid: str) -> list[dict[str, Any]]:
        raise RuntimeError("source down")

    monkeypatch.setattr(bot_nudges, "_late_payment_nudges", boom)
    with pytest.raises(RuntimeError, match="source down"):
        bot_nudges.generate_pending("u-1")
    assert captured_inserts == []


# ---------------------------------------------------------------------------
# /creator/bot route: one profile read, page still renders the same data
# ---------------------------------------------------------------------------


def test_manager_page_reads_profile_discover_and_bookings_once(
    monkeypatch, client: TestClient, counted_reads, captured_inserts
) -> None:
    _signed_in(client)
    monkeypatch.setattr(creator_routes.bot, "list_messages", lambda uid: [])
    monkeypatch.setattr(creator_routes.deal_events, "list_recent", lambda uid, limit: [])

    response = client.get("/creator/bot")

    assert response.status_code == 200
    # Route read + awareness hot-drop reader used to read it twice.
    assert counted_reads["profile"] == ["u-1"]
    assert len(counted_reads["discover"]) == 1
    assert [kw["limit"] for kw in counted_reads["bookings"]] == [20]
    snap = babyg_awareness._CACHE["u-1"][1]
    assert snap["fresh_discover_match"]["card_id"] == "op-1"
    assert snap["pending_booking"]["id"] == "bk-1"


def test_manager_page_reads_after_nudges_run_concurrently(monkeypatch, client: TestClient) -> None:
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-01-01T00:00:00Z", "full_name": "Maya"},
    )
    monkeypatch.setattr(creator_routes.bot_nudges, "generate_pending", lambda uid: [])
    barrier = threading.Barrier(5, timeout=_TIMEOUT)

    def waits(value: Any):
        def _run(*_a: Any, **_k: Any) -> Any:
            barrier.wait()
            return value

        return _run

    monkeypatch.setattr(creator_routes.bot, "list_messages", waits([]))
    monkeypatch.setattr(creator_routes.babyg_awareness, "snapshot", waits({}))
    monkeypatch.setattr(creator_routes.manager_activity, "list_recent_activity", waits([]))
    monkeypatch.setattr(creator_routes.manager_activity, "has_new_since", waits(False))
    monkeypatch.setattr(creator_routes.deal_events, "list_recent", waits([]))

    response = client.get("/creator/bot")
    assert response.status_code == 200
    assert not barrier.broken


def test_manager_page_keeps_read_failure_behavior(monkeypatch, client: TestClient) -> None:
    """Guarded reads still degrade to their defaults; the render read
    still fails the request, exactly as before."""
    _signed_in(client)
    monkeypatch.setattr(
        creator_routes.profiles, "get_creator_profile",
        lambda uid: {"onboarding_completed_at": "2026-01-01T00:00:00Z", "full_name": "Maya"},
    )
    monkeypatch.setattr(creator_routes.bot_nudges, "generate_pending", lambda uid: [])
    monkeypatch.setattr(creator_routes.deal_events, "list_recent", lambda uid, limit: [])
    monkeypatch.setattr(creator_routes.bot, "list_messages", lambda uid: [])

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(creator_routes.babyg_awareness, "snapshot", boom)
    monkeypatch.setattr(creator_routes.manager_activity, "list_recent_activity", boom)
    monkeypatch.setattr(creator_routes.manager_activity, "has_new_since", boom)
    assert client.get("/creator/bot").status_code == 200

    monkeypatch.setattr(creator_routes.bot, "list_messages", boom)
    failing = TestClient(client.app, raise_server_exceptions=False)
    _signed_in(failing)
    assert failing.get("/creator/bot").status_code == 500
