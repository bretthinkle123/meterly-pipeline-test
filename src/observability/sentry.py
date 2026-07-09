"""Sentry error tracking wiring — release-tagged to the deploy commit SHA
(the same SHA `build-provenance` signs and `deploy.yml` rolls out).

A `before_send` hook strips the API key / `Authorization` header and
`customer_id` at the SDK boundary, so PII/secrets never leave the process
even if an exception's context happens to carry them.
"""

from typing import Any

import sentry_sdk

from src.config.settings import Settings

_STRIPPED_KEYS = frozenset({"authorization", "api_key", "customer_id", "secret", "secret_hash"})


def _strip_sensitive_context(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any]:
    """Redact known-sensitive keys from the outgoing Sentry event's request context."""
    request_context = event.get("request")
    if isinstance(request_context, dict):
        headers = request_context.get("headers")
        if isinstance(headers, dict):
            for key in list(headers.keys()):
                if key.lower() in _STRIPPED_KEYS:
                    headers[key] = "***redacted***"
        data = request_context.get("data")
        if isinstance(data, dict):
            for key in list(data.keys()):
                if key.lower() in _STRIPPED_KEYS:
                    data[key] = "***redacted***"
    return event


def configure_sentry(settings: Settings) -> None:
    """Initialize the Sentry SDK if `sentry_dsn` is configured.

    A no-op when unset, so local/dev/smoke runs never require a DSN.
    """
    if not settings.sentry_dsn:
        return

    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        environment=settings.environment,
        release=settings.release_sha,
        before_send=_strip_sensitive_context,
        traces_sample_rate=0.1,
    )
