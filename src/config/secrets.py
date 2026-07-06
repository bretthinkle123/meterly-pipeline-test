"""Runtime secrets facade — the only module that talks to AWS Secrets Manager / SSM.

Business logic never calls `boto3` or reads a secret value from `os.environ`
directly (`secrets-management`). Bootstrap env vars only locate *which* secret
to fetch (see `Settings`); the value itself is retrieved here, cached briefly
in-process, and re-fetched on TTL expiry so rotation is picked up without a
redeploy.
"""

import os
import time
from dataclasses import dataclass
from typing import Any

import boto3
from botocore.exceptions import ClientError

from src.config.settings import get_settings

_CACHE_TTL_SECONDS = 600


@dataclass
class _CachedSecret:
    """A secret value cached in-process with the time it was fetched."""

    value: str
    fetched_at: float


class SecretsFacade:
    """Fetches and caches runtime secrets from AWS Secrets Manager, with an
    environment-variable fallback for local development.

    Never call `boto3` directly from anywhere else in the codebase — route
    every secret read through `get_secret()` so caching, fallback, and
    redaction stay centralized.
    """

    def __init__(self) -> None:
        self._cache: dict[str, _CachedSecret] = {}
        self._client: Any | None = None

    def _client_or_create(self) -> Any:
        """Lazily create the Secrets Manager client (avoids a network call at import time)."""
        if self._client is None:
            self._client = boto3.client(
                "secretsmanager", region_name=get_settings().aws_region
            )
        return self._client

    def get_secret(self, secret_name: str, *, env_fallback: str | None = None) -> str:
        """Return the current value of `secret_name`, fetching from Secrets
        Manager (with a short-TTL in-process cache) or `env_fallback` for local
        development.

        Raises `RuntimeError` if the secret cannot be resolved anywhere — this
        facade never falls back to a hardcoded default.
        """
        cached = self._cache.get(secret_name)
        if cached is not None and (time.monotonic() - cached.fetched_at) < _CACHE_TTL_SECONDS:
            return cached.value

        value = self._fetch_from_manager(secret_name)
        if value is None and env_fallback:
            value = os.environ.get(env_fallback)
        if value is None:
            raise RuntimeError(
                f"secret '{secret_name}' could not be resolved from Secrets Manager "
                f"or the '{env_fallback}' environment fallback"
            )

        self._cache[secret_name] = _CachedSecret(value=value, fetched_at=time.monotonic())
        return value

    def _fetch_from_manager(self, secret_name: str) -> str | None:
        """Attempt a live Secrets Manager lookup; returns None on any failure
        so local/dev environments without AWS credentials can fall back."""
        try:
            response = self._client_or_create().get_secret_value(SecretId=secret_name)
        except (ClientError, Exception):  # noqa: BLE001 - any AWS/credential failure falls through
            return None
        return response.get("SecretString")


_facade = SecretsFacade()


def get_secret(secret_name: str, *, env_fallback: str | None = None) -> str:
    """Module-level convenience wrapper around the process-wide `SecretsFacade`."""
    return _facade.get_secret(secret_name, env_fallback=env_fallback)


def get_database_url() -> str:
    """Return the database connection string via the secrets facade."""
    settings = get_settings()
    return get_secret(
        settings.database_secret_name,
        env_fallback=settings.database_secret_env_fallback,
    )
