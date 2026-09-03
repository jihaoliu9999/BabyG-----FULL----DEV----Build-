"""Tests for the creator-tabbar badge globals.

pending_action_count and unread_dm_count are template globals wired
into every render so the tabbar picks them up without every route
having to pass them in context. They must:

- return 0 for anon / non-creator sessions
- cache on request.state so the tabbar doesn't hit supabase twice
- swallow any failure to 0 so a supabase blip never blanks the nav
"""

from __future__ import annotations

from types import SimpleNamespace

from app.core import templating


class _FakeRequest:
    def __init__(self):
        self.state = SimpleNamespace()


def test_pending_action_count_zero_for_anon(monkeypatch) -> None:
    monkeypatch.setattr(templating, "read_session", lambda req: None, raising=False)
    from app.core import security

    monkeypatch.setattr(security, "read_session", lambda req: None)
    req = _FakeRequest()
    assert templating._pending_action_count(req) == 0


def test_pending_action_count_zero_for_brand(monkeypatch) -> None:
    from app.core import security

    monkeypatch.setattr(
        security, "read_session", lambda req: {"role": "brand", "user_id": "b1"}
    )
    req = _FakeRequest()
    assert templating._pending_action_count(req) == 0


def test_pending_action_count_calls_service_for_creator(monkeypatch) -> None:
    from app.core import security
    from app.services import action_proposals

    monkeypatch.setattr(
        security, "read_session", lambda req: {"role": "creator", "user_id": "c1"}
    )
    monkeypatch.setattr(
        action_proposals, "count_pending_for_user", lambda user_id: 4
    )
    req = _FakeRequest()
    assert templating._pending_action_count(req) == 4


def test_pending_action_count_caches_on_request_state(monkeypatch) -> None:
    from app.core import security
    from app.services import action_proposals

    calls = {"n": 0}

    def _count(user_id):
        calls["n"] += 1
        return 2

    monkeypatch.setattr(
        security, "read_session", lambda req: {"role": "creator", "user_id": "c1"}
    )
    monkeypatch.setattr(action_proposals, "count_pending_for_user", _count)

    req = _FakeRequest()
    templating._pending_action_count(req)
    templating._pending_action_count(req)
    assert calls["n"] == 1


def test_pending_action_count_swallows_service_error(monkeypatch) -> None:
    from app.core import security
    from app.services import action_proposals

    def _boom(**kwargs):
        raise RuntimeError("supabase down")

    monkeypatch.setattr(
        security, "read_session", lambda req: {"role": "creator", "user_id": "c1"}
    )
    monkeypatch.setattr(action_proposals, "count_pending_for_user", _boom)
    req = _FakeRequest()
    assert templating._pending_action_count(req) == 0


def test_unread_dm_count_zero_for_anon(monkeypatch) -> None:
    from app.core import security

    monkeypatch.setattr(security, "read_session", lambda req: None)
    req = _FakeRequest()
    assert templating._unread_dm_count(req) == 0


def test_unread_dm_count_calls_service_for_creator(monkeypatch) -> None:
    from app.core import security
    from app.services import dms

    monkeypatch.setattr(
        security, "read_session", lambda req: {"role": "creator", "user_id": "c1"}
    )
    monkeypatch.setattr(dms, "unread_count_for_user", lambda uid: 7)
    req = _FakeRequest()
    assert templating._unread_dm_count(req) == 7


def test_unread_dm_count_caches_on_request_state(monkeypatch) -> None:
    from app.core import security
    from app.services import dms

    calls = {"n": 0}

    def _count(uid):
        calls["n"] += 1
        return 1

    monkeypatch.setattr(
        security, "read_session", lambda req: {"role": "creator", "user_id": "c1"}
    )
    monkeypatch.setattr(dms, "unread_count_for_user", _count)

    req = _FakeRequest()
    templating._unread_dm_count(req)
    templating._unread_dm_count(req)
    assert calls["n"] == 1
