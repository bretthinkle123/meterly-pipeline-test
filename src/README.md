# src/

## Purpose

The main application package for the Meterly metering engine. Contains the FastAPI application entry point, API route handlers, domain services, data repositories, and supporting infrastructure (auth, logging, observability, database, configuration).

## Modules

| Directory / Module | Responsibility |
|---|---|
| `main.py` | FastAPI application factory; middleware registration; lifespan startup/shutdown (DB pool, Redis, OTel/Sentry init). |
| `api/` | HTTP API layer: routes, schemas, error handlers, middleware (request ID, security headers, CORS). Public surface for all incoming HTTP requests. |
| `services/` | Business logic layer: event ingestion, usage aggregation, time-window calculations. Orchestrates repositories and enforces invariants (idempotency, counter atomicity). |
| `repositories/` | Data access layer: SQL queries wrapped with parameterization, RLS scoping, and minimal-field projections. All database I/O is here. |
| `auth/` | Authentication and authorization: API key verification (split-token, Argon2id verify cache), Tier-1 and Tier-2 rate limiting (Redis token buckets). |
| `logging/` | Structured logging facade (structlog): request middleware, PII redaction, observability correlation. |
| `observability/` | OTel and Sentry integration: trace/span management, release tagging, error scrubbing. |
| `config/` | Configuration and secrets management: environment variables, dependency injection, Secrets Manager fetch. |
| `db/` | Database connectivity: async SQLAlchemy session factory, RLS scoping, connection pooling. |
| `crypto/` | Cryptographic utilities: Argon2id hashing, constant-time comparison, credential generation. |

## Relationships

**Layered architecture:**
- **Request flow (inbound):** HTTP request → `main.py` (middleware stack) → `api/` routes → `auth/` guards (rate limit, API key verify) → request handler → `services/` business logic → `repositories/` SQL → database.
- **Response flow (outbound):** Database result → `repositories/` minimal projection → `services/` aggregate/format → `api/` routes → `api/errors.py` error envelope → HTTP response (via middleware: add correlation/security headers).

**Public surfaces (facades):**
- `api.routes` — HTTP handlers; imported by `main.py` to register with FastAPI. Entry point for all external callers.
- `auth` — `require_api_key` guard; imported by routes to gate handler execution. Enforces two rate-limit tiers.
- `logging.get_logger` — centralized structured logging facade with PII redaction. Imported by every module that logs.
- `config.settings` — environment configuration singleton; imported at startup and by modules needing env values.
- `db.session_context` — async session factory for transactional scope. Imported by services and repositories.

**Dependencies (inbound imports):**
- `repositories` are imported only by `services` (single-responsibility: services orchestrate the data layer).
- `services` are imported only by `api.routes` (single-responsibility: routes dispatch to business logic).
- `auth` is imported by `main.py` (to register guards) and by `api.routes` (to call the guard).
- `logging`, `config`, `db` are imported by multiple modules as cross-cutting infrastructure.

## Notes

**Environment variables** consumed by `config/settings.py`:
- `DATABASE_URL` — PostgreSQL connection string (async, with app-role credentials).
- `REDIS_URL` — ElastiCache Redis URL for rate limiting.
- `SENTRY_DSN` — Sentry error-tracking endpoint (omit to disable; non-gating).
- `OTEL_EXPORTER_OTLP_ENDPOINT` — OTel collector endpoint (ADOT sidecar in prod; omit to disable).
- `LOG_LEVEL` — structlog verbosity (default: INFO).
- `RELEASE_SHA` — commit SHA (set by the container build; used for Sentry/X-Ray tagging).

**Deployment contract:**
- The container entrypoint runs `uvicorn src.main:app` (see `Dockerfile`).
- At startup (`lifespan`), all async resources are initialized (DB, Redis, OTel, Sentry).
- At shutdown (SIGTERM), resources drain gracefully (connection close, request drain, cleanup).
- `/health` (liveness) requires no dependencies and can be called at any time.
- `/health/ready` (readiness) requires the DB and migration head to be intact.
