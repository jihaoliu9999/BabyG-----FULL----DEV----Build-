"""Prime creator-tabbar template globals on request.state.

The mobile creator tabbar (``app/templates/_partials/creator_tabbar.html``)
renders two Supabase-backed badges via the template globals
``pending_action_count`` and ``unread_dm_count`` (registered in
``app/core/templating.py``). Each global checks
``request.state.__dict__`` for a cached ``int`` and only falls back to
its Supabase source on a miss. Before this dependency landed, the
``/creator`` dashboard route was the only handler that primed both
values on ``request.state``; every other authenticated creator
route paid three uncached round-trips per render:

- ``action_proposals.count_pending_for_user`` (1 read)
- ``dms.unread_count_for_user`` (2 postgrest calls internally)
- ``instagram_dms.unread_count_for_creator`` (1 read)

This module exposes ``prime_creator_tabbar``, a FastAPI dependency
attached at the creator, discover, and opportunities routers in
``app/main.py``. It fires the same reads exactly once per creator
GET request and stores the results on ``request.state`` so the
template globals resolve from the cache. Behavior contract:

- POST/PUT/DELETE requests skip priming — those routes redirect and
  never render the tabbar, so priming would add three avoidable
  reads to every form submit.
- The ``/creator`` dashboard path skips priming — its route body
  fetches richer data (the list of pending actions, split native/IG
  unread counts) that it uses for both the badges and Home content,
  and it already primes the same request.state keys. Skipping avoids
  a duplicated read on the hottest page.
- Non-creator sessions (anonymous, brand, operator, unresolved)
  short-circuit — the template globals themselves return 0 for those
  roles, so priming them would fire zero-value reads for nothing.
- Never raises. On any Supabase or session-decode failure the count
  defaults to 0, exactly like the underlying template globals
  themselves already do (``app/core/templating.py:400-422`` and
  ``:435-450``), so a flaky read never blanks a page.
- Idempotent: if some other layer has already populated a value on
  ``request.state``, priming leaves it alone.

The dependency does not cache anything across requests — every
new request gets a fresh ``request.state`` from Starlette. The
template global cache lives for the duration of a single request.
"""

from __future__ import annotations

import logging

from fastapi import Request

from app.core.security import read_session

logger = logging.getLogger(__name__)

# The dashboard route primes both counts itself from richer data it
# already fetches in its own ``asyncio.gather``. Skipping this exact
# path avoids priming firing the same reads a moment before the
# dashboard's own assignments overwrite them.
_DASHBOARD_PATH = "/creator"


def _has_cached_int(request: Request, attr: str) -> bool:
    """True when ``request.state.<attr>`` is already a real int.

    Mirrors ``app/core/templating.py::_cached_state_int`` — Starlette's
    ``State`` stores writes inside an internal ``_state`` dict, but
    simpler test doubles use ``__dict__``. Try both so a fake
    ``SimpleNamespace`` and a real ``Request.state`` both work. A
    MagicMock exposes neither as a real dict, so both checks
    short-circuit to False, preserving the defensive behavior.
    """
    state = getattr(request, "state", None)
    if state is None:
        return False
    for slot in ("_state", "__dict__"):
        stash = getattr(state, slot, None)
        if isinstance(stash, dict) and isinstance(stash.get(attr), int):
            return True
    return False


def _store_int(request: Request, attr: str, value: int) -> None:
    """Best-effort ``setattr(request.state, attr, int(value))``.

    Never raises: a missing state or an int-cast failure silently
    no-ops so a flaky underlying value cannot break the render.
    """
    state = getattr(request, "state", None)
    if state is None:
        return
    try:
        setattr(state, attr, int(value))
    except (TypeError, ValueError):
        return
    except Exception:
        # Defensive against a state object with a custom __setattr__
        # that raises (unlikely under Starlette, but the template
        # globals apply the same guard).
        return


def prime_creator_tabbar(request: Request) -> None:
    """Prime ``request.state.pending_action_count`` and
    ``request.state.unread_dm_count`` once per authenticated creator
    GET request. See module docstring for full contract.

    This function is a FastAPI dependency; attach with
    ``dependencies=[Depends(prime_creator_tabbar)]`` on
    ``app.include_router(...)`` for every router that serves creator
    pages.
    """
    # POSTs redirect and never render the tabbar; skipping them saves
    # 3 reads per form submit.
    if request.method != "GET":
        return

    # Dashboard primes both values itself in its own gather, from
    # richer data it fetches for the Home rail + primary carousel.
    if request.url.path == _DASHBOARD_PATH:
        return

    # If both keys are already primed we're done. Some future
    # handler that already primes on its own will get here after
    # the dependency runs; the dependency only fills what's missing.
    if _has_cached_int(request, "pending_action_count") and _has_cached_int(
        request, "unread_dm_count"
    ):
        return

    # Session-decode never raises to the caller. A bad or missing
    # session falls through as "no user" and the template globals
    # will render 0 badges — same behavior as before this dependency.
    try:
        session = read_session(request)
    except Exception:
        session = None
    if not session or session.get("role") != "creator":
        return
    user_id = session.get("user_id")
    if not isinstance(user_id, str) or not user_id:
        return

    if not _has_cached_int(request, "pending_action_count"):
        try:
            # Local import matches how ``templating.py`` does it:
            # keeps the module graph linear at import time and lets
            # tests monkeypatch either the service module or this
            # dependency in isolation.
            from app.services import action_proposals

            count = int(
                action_proposals.count_pending_for_user(user_id=user_id) or 0
            )
        except Exception:
            # Same default the template global itself uses on error.
            count = 0
        _store_int(request, "pending_action_count", count)

    if not _has_cached_int(request, "unread_dm_count"):
        try:
            from app.services import dms, instagram_dms

            native = int(dms.unread_count_for_user(user_id) or 0)
            ig = int(instagram_dms.unread_count_for_creator(user_id) or 0)
            total = native + ig
        except Exception:
            total = 0
        _store_int(request, "unread_dm_count", total)
