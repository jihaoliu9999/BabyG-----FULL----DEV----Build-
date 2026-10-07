"""Read sharing + concurrent fan-out for the babyg Manager load.

Opening /creator/bot used to run every Supabase read one after another,
and a few of them twice: the nudge generator and the awareness snapshot
each asked for the same discover feed, the same upcoming bookings, and
(via the hot-drop reader) the creator profile the route already had.
Two small tools fix that without changing what any reader returns:

* ``run_parallel(*calls)`` runs independent, read-only zero-arg calls
  on a short-lived thread pool and returns their results in call
  order. Each call runs in a copy of the caller's context (the same
  thing ``asyncio.to_thread`` does), so logging and Sentry keep the
  request context. If calls raise, the first exception in call order is
  re-raised once every call has finished — what the sequential code
  surfaced.

* ``scope()`` + ``shared(key, read)``: inside one Manager load, an
  identical read asked for by two callers runs once and each caller
  gets its own copy of the result. Outside a scope ``shared`` is a
  plain call, so every other caller is untouched. Keys always carry the
  user id, the scope lives in a ContextVar that is set per request and
  reset when the load finishes, so nothing is shared across requests
  or users and nothing outlives the request. A shared read that raises
  is never reused: the next caller makes its own attempt, exactly as it
  would have before.
"""

from __future__ import annotations

import contextvars
import copy
import threading
from collections.abc import Callable, Hashable, Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any

# Largest upcoming-bookings slice any Manager-load caller asks for
# (bot_nudges reads 20; the awareness snapshot reads 6 and 12). The
# query orders by starts_at, so its first N rows are the limit=N result.
UPCOMING_BOOKINGS_LIMIT = 20


class _Scope:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.entries: dict[Hashable, Future[Any]] = {}

    def seed(self, values: dict[Hashable, Any] | None) -> None:
        for key, value in (values or {}).items():
            with self.lock:
                if key in self.entries:
                    continue
                done: Future[Any] = Future()
                done.set_result(value)
                self.entries[key] = done


_SCOPE: contextvars.ContextVar[_Scope | None] = contextvars.ContextVar(
    "babyg_manager_reads", default=None
)


@contextmanager
def scope(seed: dict[Hashable, Any] | None = None) -> Iterator[None]:
    """Share identical reads for the duration of the block.

    Re-entrant: opened inside an existing scope it joins that scope
    (``seed`` values are added if their keys are not already there).
    """
    current = _SCOPE.get()
    if current is not None:
        current.seed(seed)
        yield
        return
    fresh = _Scope()
    fresh.seed(seed)
    token = _SCOPE.set(fresh)
    try:
        yield
    finally:
        _SCOPE.reset(token)


def shared(key: Hashable, read: Callable[[], Any]) -> Any:
    """Return ``read()``, running it at most once per key per scope.

    Concurrent callers of the same key wait for the first one. Every
    caller receives a deep copy, so no reader can mutate another
    reader's data. If the shared read raised, a waiting caller falls
    back to its own ``read()``.
    """
    current = _SCOPE.get()
    if current is None:
        return read()
    with current.lock:
        pending = current.entries.get(key)
        owner = pending is None
        if pending is None:
            pending = Future()
            current.entries[key] = pending
    if owner:
        try:
            value = read()
        except BaseException as exc:
            with current.lock:
                current.entries.pop(key, None)
            pending.set_exception(exc)
            raise
        pending.set_result(value)
        return copy.deepcopy(value)
    try:
        value = pending.result()
    except BaseException:
        return read()
    return copy.deepcopy(value)


def run_parallel(*calls: Callable[[], Any]) -> list[Any]:
    """Run independent read-only calls concurrently; results in call order."""
    if len(calls) < 2:
        return [call() for call in calls]
    with ThreadPoolExecutor(
        max_workers=len(calls), thread_name_prefix="babyg-manager-read"
    ) as pool:
        futures = [pool.submit(contextvars.copy_context().run, call) for call in calls]
    # Leaving the ``with`` waited for every call; ``result()`` re-raises
    # in call order, so the first failing call wins as it did sequentially.
    return [future.result() for future in futures]


# ---------------------------------------------------------------------------
# The reads the Manager load asks for more than once. Each helper is the
# exact call its callers made before; inside a scope it is shared.
# ---------------------------------------------------------------------------


def creator_profile_key(user_id: str) -> tuple[str, str]:
    return ("creator_profile", user_id)


def creator_profile(user_id: str) -> Any:
    """``profiles.get_creator_profile(user_id)``."""
    from app.services import profiles

    return shared(
        creator_profile_key(user_id), lambda: profiles.get_creator_profile(user_id)
    )


def discover_matches(user_id: str) -> Any:
    """``discover.list_cards(viewer_id=user_id, viewer_role="creator", kind="all", limit=6)``."""
    from app.services import discover

    return shared(
        ("discover.matches", user_id),
        lambda: discover.list_cards(
            viewer_id=user_id, viewer_role="creator", kind="all", limit=6
        ),
    )


def upcoming_bookings(user_id: str, *, limit: int) -> Any:
    """``bookings.list_for_user(user_id, horizon="upcoming", limit=limit)``."""
    from app.services import bookings

    if _SCOPE.get() is None or limit > UPCOMING_BOOKINGS_LIMIT:
        return bookings.list_for_user(user_id, horizon="upcoming", limit=limit)
    rows = shared(
        ("bookings.upcoming", user_id),
        lambda: bookings.list_for_user(
            user_id, horizon="upcoming", limit=UPCOMING_BOOKINGS_LIMIT
        ),
    )
    return rows[:limit]
