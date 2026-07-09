"""Auth facade — `require_api_key` is the only guard route handlers depend on.

Resolves the exact tension the plan calls out: Argon2id is intentionally slow
(tens of ms), so running it on every request at the target 500 req/s would
blow the ingest p95 budget. An in-process verification cache, keyed by the
token's public `key_id` and guarded by a constant-time digest comparison
(ASVS 11.2.4 — in-scope L3), lets a repeat caller on the same task skip the
Argon2id verify after the first request while the durable at-rest store stays
Argon2id-only. See the plan's "Argon2id-vs-p95 tension" section for the full
tradeoff writeup, including the accepted ~5 min revocation-latency risk (Q4).
"""

import time
from dataclasses import dataclass

from fastapi import Depends, HTTPException, Request, status

from src.auth.api_key import AuthenticatedPrincipal, parse_split_token, verify_api_key
from src.config.settings import get_settings
from src.crypto import constant_time_equals, sha256_digest
from src.db.session import get_engine
from src.logging import get_logger

logger = get_logger(service="meterly")

_BEARER_PREFIX = "Bearer "


@dataclass
class _CachedVerification:
    """An in-process cache entry: the digest of the secret that was last
    verified for this `key_id`, and the resolved principal."""

    secret_digest: str
    principal: AuthenticatedPrincipal
    cached_at: float


class ApiKeyVerificationCache:
    """Per-process cache mapping a token's public `key_id` to its last-verified
    secret digest and resolved principal, with a TTL.

    Keyed on the *public* `key_id` (not a secret) so the cache lookup itself
    leaks nothing; the constant-time digest comparison is what actually
    authorizes a cache hit.
    """

    def __init__(self, ttl_seconds: int) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[str, _CachedVerification] = {}

    def get(self, key_id: str, secret_digest: str) -> AuthenticatedPrincipal | None:
        """Return the cached principal if `key_id` has a fresh, matching entry."""
        entry = self._entries.get(key_id)
        if entry is None:
            return None
        if (time.monotonic() - entry.cached_at) > self._ttl_seconds:
            del self._entries[key_id]
            return None
        if not constant_time_equals(secret_digest, entry.secret_digest):
            return None
        return entry.principal

    def put(self, key_id: str, secret_digest: str, principal: AuthenticatedPrincipal) -> None:
        """Cache a freshly Argon2id-verified principal for `key_id`."""
        self._entries[key_id] = _CachedVerification(
            secret_digest=secret_digest, principal=principal, cached_at=time.monotonic()
        )

    def invalidate(self, key_id: str) -> None:
        """Drop a cached entry (used by tests and any future revocation channel)."""
        self._entries.pop(key_id, None)


_verification_cache: ApiKeyVerificationCache | None = None


def _get_verification_cache() -> ApiKeyVerificationCache:
    """Return the process-wide verification cache, created on first use."""
    global _verification_cache
    if _verification_cache is None:
        _verification_cache = ApiKeyVerificationCache(get_settings().api_key_cache_ttl_seconds)
    return _verification_cache


def _extract_bearer_token(request: Request) -> str | None:
    """Pull the raw bearer token out of the `Authorization` header, if present and well-formed."""
    header_value = request.headers.get("authorization")
    if not header_value or not header_value.startswith(_BEARER_PREFIX):
        return None
    return header_value[len(_BEARER_PREFIX) :]


async def require_api_key(request: Request) -> AuthenticatedPrincipal:
    """FastAPI dependency: authenticate the request's API key.

    Cache hit path: constant-time digest compare only (no Argon2id). Cache
    miss: split-token parse + DB lookup + Argon2id verify, then populate the
    cache. Any failure raises 401 — malformed header, unknown/revoked key, and
    a wrong secret are all indistinguishable to the caller.
    """
    presented_key = _extract_bearer_token(request)
    if presented_key is None:
        logger.warning("apikey.auth", userId=None, action="deny", reason="missing_or_malformed_header")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required")

    parsed = parse_split_token(presented_key)
    if parsed is None:
        logger.warning("apikey.auth", userId=None, action="deny", reason="malformed_token")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    secret_digest = sha256_digest(presented_key)
    cache = _get_verification_cache()
    cached_principal = cache.get(parsed.key_id, secret_digest)
    if cached_principal is not None:
        logger.info("apikey.auth", userId=cached_principal.api_key_id, action="allow", reason="cache_hit")
        return cached_principal

    async with get_engine().connect() as connection:
        principal = await verify_api_key(connection, presented_key)

    if principal is None:
        logger.warning("apikey.auth", userId=None, action="deny", reason="verification_failed")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid API key")

    cache.put(parsed.key_id, secret_digest, principal)
    logger.info("apikey.auth", userId=principal.api_key_id, action="allow", reason="verified")
    return principal


ApiKeyPrincipal = Depends(require_api_key)
