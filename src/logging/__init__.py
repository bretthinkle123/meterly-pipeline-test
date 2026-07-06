"""Structured logging facade — `get_logger()` is the only way application code
should obtain a logger.

Configures structlog once at import time to emit JSON to stdout (ECS ships
stdout to CloudWatch with no further app change) and applies a centralized
redaction processor so a secret or raw PII value can never leave the process
through a log line, regardless of which call site logs it.
"""

import logging as stdlib_logging
import sys
from typing import Any

import structlog

_REDACTED = "***redacted***"

# Field names that must never appear in raw form in a log event — the API-key
# secret/Authorization header and any raw customer identifier (personal data
# class per data-protection-conventions). Matched case-insensitively against
# structlog event-dict keys.
_SENSITIVE_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "api_key_secret",
        "secret",
        "secret_hash",
        "password",
        "customer_id",
        "token",
    }
)


def _redact_sensitive_fields(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Strip sensitive values from a structlog event dict before it is rendered.

    This is the centralized PII/secret-redaction control every log call
    inherits automatically — call sites never redact ad hoc.
    """
    for key in list(event_dict.keys()):
        if key.lower() in _SENSITIVE_KEYS:
            event_dict[key] = _REDACTED
    return event_dict


def _strip_control_characters(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Neutralize newlines/control characters in string values to prevent log forging."""
    for key, value in event_dict.items():
        if isinstance(value, str):
            event_dict[key] = value.replace("\r", "\\r").replace("\n", "\\n")
    return event_dict


def _configure_structlog() -> None:
    """Configure structlog once at import time: JSON renderer, ISO timestamps,
    the redaction/anti-forging processors, and stdlib logging routed through it."""
    stdlib_logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=stdlib_logging.INFO
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
            _redact_sensitive_fields,
            _strip_control_characters,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(stdlib_logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


_configure_structlog()


def get_logger(**initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return the single configured structlog logger, optionally bound with
    initial context values (e.g. `service=...`).

    Every module imports this rather than instantiating its own logger, so
    redaction and field conventions stay centralized (`code-standards` facade
    rule).
    """
    return structlog.get_logger(**initial_values)
