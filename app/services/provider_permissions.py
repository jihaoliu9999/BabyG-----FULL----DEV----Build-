"""Provider connection and OAuth-scope checks for approved actions."""

from __future__ import annotations

from dataclasses import dataclass

from app.services import oauth_connections


@dataclass(frozen=True)
class ProviderPermissionCheck:
    ok: bool
    reason: str | None = None
    missing_scopes: tuple[str, ...] = ()


def check_provider_access(
    *,
    user_id: str,
    provider: str | None,
    required_scopes: list[str] | tuple[str, ...],
) -> ProviderPermissionCheck:
    """Verify the creator has the provider connection and scopes needed.

    This is intentionally separate from provider API clients. Future external
    executors should call this before they call Gmail, Instagram, Calendar, or
    any other external write integration.
    """
    scopes = tuple(scope for scope in required_scopes if scope)
    if not provider:
        if scopes:
            return ProviderPermissionCheck(ok=False, reason="provider_required")
        return ProviderPermissionCheck(ok=True)
    if provider == "google":
        connection = oauth_connections.get_google_connection(user_id)
        if not connection:
            return ProviderPermissionCheck(ok=False, reason="provider_not_connected")
        granted = {
            str(scope)
            for scope in (connection.get("scopes") or [])
            if str(scope).strip()
        }
        missing = tuple(scope for scope in scopes if scope not in granted)
        if missing:
            return ProviderPermissionCheck(
                ok=False,
                reason="missing_required_scope",
                missing_scopes=missing,
            )
        return ProviderPermissionCheck(ok=True)
    return ProviderPermissionCheck(ok=False, reason="unsupported_provider")
