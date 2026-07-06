# src/observability/

## Purpose

Observability instrumentation: OpenTelemetry trace export (to X-Ray via ADOT sidecar), Sentry error tracking with release tagging and PII scrubbing.

## Modules

| File / Module | Responsibility |
|---|---|
| `otel.py` | OpenTelemetry initialization: sets up OTLP exporter (to ADOT sidecar), configures trace processors, tags spans with `deployment.environment` and `service.version`. |
| `sentry.py` | Sentry initialization: sets up the SDK with release tagging, configures a `before_send` hook to scrub PII/secrets from error payloads before shipping. |

## Relationships

**Public surface:**
- Both modules are initialized at app startup in `src/main.py` (lifespan context manager).
- `configure_otel()` and `configure_sentry()` are called once during app construction.
- No public entry points; configuration happens at the module level.

**Dependencies:**
- `otel.py` imports OpenTelemetry SDK and the OTLP exporter.
- `sentry.py` imports the Sentry SDK.
- Both import `src.config.settings` for endpoint URLs and the release SHA.

**Instrumentation:**
- After `configure_otel()` is called, all async I/O operations (database, HTTP requests) are automatically traced by the SDK's instrumentor libraries.
- OTel traces are exported to the ADOT sidecar (expected at `OTEL_EXPORTER_OTLP_ENDPOINT`, e.g., `http://localhost:4317`).
- The sidecar forwards traces to AWS X-Ray.
- Trace data includes span duration, status (success/error), attributes (e.g., `db.statement`, `http.status_code`).

**Error reporting:**
- Unhandled exceptions propagate to Sentry (if configured and enabled).
- The `before_send` hook in Sentry's configuration scrubs PII fields (same redaction as structured logging: `customer_id`, `secret`, etc.).
- Release is tagged with the commit SHA (`settings.release_sha`), so errors are tracked per deployment.

## Notes

**Optional configuration:**
- If `SENTRY_DSN` is not set, Sentry is disabled (no-op initialization).
- If `OTEL_EXPORTER_OTLP_ENDPOINT` is not set, OTel exports are disabled (no-op).
- Both are non-gating: the app runs fine without them (traces and errors are just not collected).

**Environment variables:**
- `OTEL_EXPORTER_OTLP_ENDPOINT` — ADOT sidecar endpoint (default: `http://localhost:4317`).
- `SENTRY_DSN` — Sentry project DSN (omit to disable).
- `RELEASE_SHA` — commit hash for release tagging (set by container build).
- `ENVIRONMENT` — deployment environment (prod/staging/local; used in trace/error context).

**PII scrubbing (before_send):**
- Sentry's `before_send` is registered with a custom hook that removes sensitive fields from error payloads.
- Matches the same field list as structured logging redaction.
- Ensures no raw `customer_id`, `secret`, or API keys leak into error tracking.
