"""`_assert_session_secret` must refuse to boot internet-reachable
environments with a weak or default session secret.

`URLSafeTimedSerializer` silently signs with an empty string, which
would let a visitor mint sessions impersonating anyone. The guard was
previously production-only; AUDIT.md M6 flagged staging as a gap.
"""

from __future__ import annotations

import pytest

from app.main import DEFAULT_DEV_SECRET, _assert_session_secret


class _StubSettings:
    def __init__(self, *, env: str, session_secret: str) -> None:
        self.env = env
        self.session_secret = session_secret


# ---------- env=dev: guard MUST short-circuit ----------


def test_dev_accepts_default_secret():
    """Local dev runs without an .env file; do not block contributors."""
    _assert_session_secret(_StubSettings(env="dev", session_secret=DEFAULT_DEV_SECRET))


def test_dev_accepts_empty_secret():
    _assert_session_secret(_StubSettings(env="dev", session_secret=""))


# ---------- env=staging: must enforce ----------


def test_staging_rejects_default_secret():
    with pytest.raises(RuntimeError) as exc:
        _assert_session_secret(
            _StubSettings(env="staging", session_secret=DEFAULT_DEV_SECRET)
        )
    assert "staging" in str(exc.value)


def test_staging_rejects_empty_secret():
    with pytest.raises(RuntimeError) as exc:
        _assert_session_secret(_StubSettings(env="staging", session_secret=""))
    assert "staging" in str(exc.value)


def test_staging_rejects_short_secret():
    with pytest.raises(RuntimeError):
        _assert_session_secret(_StubSettings(env="staging", session_secret="x" * 31))


def test_staging_accepts_strong_secret():
    _assert_session_secret(_StubSettings(env="staging", session_secret="x" * 48))


# ---------- env=production: still enforced ----------


def test_production_rejects_default_secret():
    with pytest.raises(RuntimeError):
        _assert_session_secret(
            _StubSettings(env="production", session_secret=DEFAULT_DEV_SECRET)
        )


def test_production_rejects_short_secret():
    with pytest.raises(RuntimeError):
        _assert_session_secret(
            _StubSettings(env="production", session_secret="x" * 31)
        )


def test_production_accepts_strong_secret():
    _assert_session_secret(_StubSettings(env="production", session_secret="x" * 48))
