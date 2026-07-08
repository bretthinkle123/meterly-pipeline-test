"""Resolves and memoizes the server-held `dashboard-reader` API-key
principal — the load-bearing control behind "the browser never holds an API
key" (plan §Auth).

The BFF never accepts a client credential; instead it fetches a dedicated,
least-privilege, read-only, single-tenant reader key from Secrets Manager
(via the `src.config.secrets` facade) and verifies it through the exact same
`verify_api_key` path a browser-presented key would use, so the reader is
bound by identical Argon2id-at-rest and tenant-scoping semantics as any other
key (ASVS 8.2.1/8.2.2). Memoized with a short TTL so Argon2id runs once per
TTL window rather than once per fan-out read (a `granularity=day` render can
issue up to 264 reads).
"""

import time
from dataclasses import dataclass

from src.auth.api_key import AuthenticatedPrincipal, verify_api_key
from src.config.secrets import get_secret
from src.config.settings import get_settings
from src.db.session import get_engine
from src.logging import get_logger

logger = get_logger(service="meterly")

_TTL_SECONDS = 300  # mirrors `Settings.api_key_cache_ttl_seconds` (the browser-key cache)


@dataclass
class _CachedReaderPrincipal:
    """A memoized reader principal and the time it was resolved."""

    principal: AuthenticatedPrincipal
    resolved_at: float


_cached_principal: _CachedReaderPrincipal | None = None


async def get_dashboard_reader_principal() -> AuthenticatedPrincipal:
    """Return the memoized `dashboard-reader` principal, re-resolving it from
    Secrets Manager and re-verifying it once the TTL has elapsed.

    Raises `RuntimeError` if the reader key cannot be resolved or fails
    verification — this propagates to the BFF's generic 500 error envelope
    (never a stack/secret leak, AC24) rather than serving stale/no data.
    """
    global _cached_principal
    if _cached_principal is not None and (time.monotonic() - _cached_principal.resolved_at) < _TTL_SECONDS:
        return _cached_principal.principal

    settings = get_settings()
    presented_key = get_secret(
        settings.dashboard_reader_secret_name,
        env_fallback=settings.dashboard_reader_secret_env_fallback,
    )

    async with get_engine().connect() as connection:
        principal = await verify_api_key(connection, presented_key)

    if principal is None:
        logger.error("dashboard.reader_key_invalid", action="deny")
        raise RuntimeError("dashboard-reader API key failed verification")

    _cached_principal = _CachedReaderPrincipal(principal=principal, resolved_at=time.monotonic())
    return principal


def invalidate_cached_reader_principal() -> None:
    """Drop the cached reader principal (test-only escape hatch, mirrors
    `ApiKeyVerificationCache.invalidate`)."""
    global _cached_principal
    _cached_principal = None
