"""Reusable FastAPI dependencies for auth and authorization.

Three layers, increasingly strict:

  * `current_session()` — reads the signed cookie. Returns None if absent or
    invalid. Cheap, no DB hit. Use when a route needs to know "is anyone
    signed in?" without requiring auth.

  * `current_user()` — same as current_session() but also raises 401 if not
    present. Use on every authenticated route.

  * `require_role(*roles)` — factory returning a dep that asserts the
    session role is in the allowed set. 403 if not.

We intentionally do NOT hit the DB in the per-request dep. The signed cookie
already carries the user's role. The cookie is invalidated on role change by
forcing a logout (handled in the role-change admin path, when we build it).
"""

from __future__ import annotations

from collections.abc import Iterable

from fastapi import HTTPException, Request, status

from app.core.security import SessionPayload, read_session


def current_session(request: Request) -> SessionPayload | None:
    return read_session(request)


def current_user(request: Request) -> SessionPayload:
    session = read_session(request)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="auth required")
    return session


def require_role(*roles: str):
    _validate_roles(roles)
    allowed = frozenset(roles)

    def _dep(request: Request) -> SessionPayload:
        session = current_user(request)
        if session["role"] not in allowed:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role"
            )
        return session

    return _dep


def _validate_roles(roles: Iterable[str]) -> None:
    """Sanity-check the role list at import time. Catches typos like
    `require_role("creators")` that would otherwise silently 403 every
    request to the affected route."""
    valid = {"creator", "operator", "admin"}
    for r in roles:
        if r not in valid:
            raise ValueError(f"unknown role: {r}")
