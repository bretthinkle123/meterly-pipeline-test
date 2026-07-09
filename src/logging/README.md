# src/logging/

## Purpose

Centralized structured logging facade using structlog: request middleware (correlation context), PII redaction processor, and a uniform `get_logger()` entry point for all modules.

## Modules

| File / Module | Responsibility |
|---|---|
| `__init__.py` | `get_logger(service=...)` — returns a logger instance with the given service name; applies PII redaction. Facade entry point for all logging calls. |
| `middleware.py` | Request middleware: extracts/generates request ID and trace ID (from OTel span or AWS X-Amzn-Trace-Id header), stores in request state for correlation. |

## Relationships

**Public surface:**
- `get_logger` is imported by every module that logs: `src/api`, `src/auth`, `src/services`, `src/observability`, etc.
- `middleware.py` is registered in `src/main.py` as part of the FastAPI middleware stack.

**Dependencies:**
- Imports `src.config.settings` for `LOG_LEVEL` environment variable.
- Imports `structlog` for structured logging and the PII redaction pipeline.

**PII redaction:**
- The `get_logger` facade applies a redaction processor that scrubs known PII fields from log output.
- Fields redacted: `customer_id`, `api_key_id`, `secret`, `key_id`, `idempotency_key`, and other credential-adjacent data.
- Redacted values are replaced with a placeholder (e.g., `"<redacted>"` or a hash).
- Tested in `tests/test_logging_redaction.py`: ensures no raw `customer_id` appears in any log output.

**Request correlation:**
- Request ID (unique per request) and trace ID (from OTel span context or HTTP header) are extracted by the middleware.
- Both are stored in request state (`request.state.request_id`, `request.state.trace_id`).
- All subsequent log calls include these in the structured context (via `structlog.contextvars`), so every log line is tagged with the request.
- Used for tracing and debugging multi-step request flows (e.g., cache miss → DB lookup → Argon2id verify).

**Log output format:**
- JSON lines (one JSON object per log line) to stdout.
- Consumed by the container runtime and shipped to CloudWatch.
- Includes timestamp, log level, service name, message, context fields, request ID, trace ID.

## Notes

**Thread-safe and async-safe:**
- structlog uses `contextvars` (not thread-local storage) for context propagation.
- Safe for both sync and async code (e.g., can be called from middleware, async handlers, background tasks).

**Environment variable:**
- `LOG_LEVEL` — structlog verbosity (default: INFO). Set to DEBUG for more verbose output during development.

**Custom fields:**
- Loggers can be created with a service name: `get_logger(service="meterly")`.
- Additional context can be passed per log call: `logger.info("event", key1=value1, key2=value2)`.
- All context is included in the JSON output for structured querying (CloudWatch Logs Insights, etc.).
